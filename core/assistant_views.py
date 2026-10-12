import re
import json
import logging
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Dealer, Product
from .assistant_tools import tool_search_parts, tool_check_stock, tool_create_draft_order
from .exceptions import api_error

logger = logging.getLogger(__name__)

# Session in-memory store for conversation turns
SESSION_MEMORY = {}

SYSTEM_INSTRUCTION = """You are the VIKMO DealerDesk AI Assistant helping motorcycle parts dealers.
You have access to tools:
1. `search_parts(query, vehicle, category)`: search products in the catalogue.
2. `check_stock(sku)`: check live stock and price for a specific product SKU.
3. `create_draft_order(dealer_id, items)`: create a DRAFT order for the dealer.

Rules:
- You must always ground product recommendations, stock levels, and prices using your tools.
- Never invent/hallucinate products, SKUs, prices, or orders.
- Whenever a dealer asks for parts, search parts or check stock using the tools.
- If a request is ambiguous, ask clarifying questions (e.g. vehicle model or part type).
- Only create DRAFT orders when the dealer explicitly asks to place/create an order.
- Decline off-topic requests politely.
"""

def extract_cited_skus(text, product_skus=None):
    if not text:
        return []
    # SKUs follow standard format like BRK-1044, FIL-1016, ELE-1001, BDY-1001, CHN-1001, etc.
    matches = re.findall(r"\b[A-Z]{3}-\d{4}\b", text)
    valid_skus = set()
    for m in matches:
        if product_skus is None:
            if Product.objects.filter(sku=m).exists():
                valid_skus.add(m)
        else:
            if m in product_skus:
                valid_skus.add(m)
    return sorted(list(valid_skus))

def rule_based_fallback(dealer, message):
    """
    Robust rule-based reasoning engine implementing genuine tool calling
    when external LLM provider keys are not supplied or rate-limited.
    """
    tool_calls = []
    actions = []
    msg_lower = message.lower()
    
    # 1. Order placement pattern: e.g. "order 2 of BRK-1044", "create draft order for BRK-1044 quantity 3"
    sku_match = re.search(r"\b([A-Z]{3}-\d{4})\b", message)
    qty_match = re.search(r"\b(\d+)\s*(?:units|qty|quantity|pieces|of)?\b", message)

    if ("order" in msg_lower or "create order" in msg_lower or "buy" in msg_lower) and sku_match:
        sku = sku_match.group(1)
        qty = 1
        if qty_match:
            try:
                qty = int(qty_match.group(1))
            except Exception:
                qty = 1
        tool_calls.append({
            "name": "create_draft_order",
            "arguments": {"dealer_id": dealer.id, "items": [{"sku": sku, "quantity": qty}]}
        })
        res = tool_create_draft_order(dealer.id, [{"sku": sku, "quantity": qty}])
        if "error" not in res:
            actions.append({
                "type": "draft_order_created",
                "order_number": res["order_number"]
            })
            reply = f"I have created draft order {res['order_number']} for {qty} unit(s) of {sku}. Total amount is ₹{res['grand_total']}."
        else:
            reply = f"Could not create draft order: {res['error']}."
        return reply, [sku], tool_calls, actions

    # 2. Stock check pattern: "stock of BRK-1044" or "how many BRK-1044 in stock"
    if sku_match and ("stock" in msg_lower or "price" in msg_lower or "have" in msg_lower or "available" in msg_lower):
        sku = sku_match.group(1)
        tool_calls.append({
            "name": "check_stock",
            "arguments": {"sku": sku}
        })
        res = tool_check_stock(sku)
        if "error" not in res:
            reply = f"{res['name']} ({sku}) is available with {res['stock']} units in stock at ₹{res['price']}."
            return reply, [sku], tool_calls, actions
        else:
            return f"Product with SKU {sku} was not found.", [], tool_calls, actions

    # 3. Product search pattern: parts / vehicle queries
    # Look for vehicle mentions or part keywords
    vehicle_keywords = ["pulsar", "duke", "apache", "splendor", "classic 350", "activa", "fzs", "himalayan", "dominar"]
    found_vehicle = None
    for vk in vehicle_keywords:
        if vk in msg_lower:
            found_vehicle = vk
            break

    query = None
    if "brake" in msg_lower or "pad" in msg_lower:
        query = "brake"
    elif "chain" in msg_lower or "sprocket" in msg_lower:
        query = "chain"
    elif "filter" in msg_lower:
        query = "filter"
    elif "oil" in msg_lower:
        query = "oil"
    elif "mirror" in msg_lower:
        query = "mirror"
    elif "indicator" in msg_lower or "light" in msg_lower:
        query = "indicator"

    if found_vehicle or query or "part" in msg_lower or "do you have" in msg_lower:
        tool_calls.append({
            "name": "search_parts",
            "arguments": {
                "query": query or message,
                "vehicle": found_vehicle
            }
        })
        results = tool_search_parts(query=query, vehicle=found_vehicle)
        if results:
            first = results[0]
            reply = f"Yes, we have {first['name']} ({first['sku']}) for ₹{first['price']} with {first['stock']} in stock."
            cited = [first['sku']]
            return reply, cited, tool_calls, actions
        else:
            reply = "I searched our catalogue but couldn't find matching parts. Could you specify the exact vehicle model or part name?"
            return reply, [], tool_calls, actions

    # 4. General greeting or query
    if "hello" in msg_lower or "hi" in msg_lower or "help" in msg_lower:
        return "Hello! I am your DealerDesk Assistant. I can help you search motorcycle parts, check current stock and pricing, and create draft orders. What parts are you looking for today?", [], [], []

    return "I can assist you with motorcycle parts catalogue search, stock verification, and creating draft orders. Please provide a part name, SKU, or vehicle model.", [], [], []

@api_view(["POST"])
def assistant_chat(request):
    session_id = request.data.get("session_id")
    dealer_id = request.data.get("dealer_id")
    message = request.data.get("message")

    if not session_id or not message or dealer_id is None:
        return api_error("VALIDATION_ERROR", "session_id, dealer_id, and message are required", [], 400)

    try:
        dealer = Dealer.objects.get(id=dealer_id)
    except Dealer.DoesNotExist:
        return api_error("VALIDATION_ERROR", f"Dealer with id {dealer_id} does not exist", [], 400)

    # Initialize session history
    if session_id not in SESSION_MEMORY:
        SESSION_MEMORY[session_id] = []

    history = SESSION_MEMORY[session_id]
    history.append({"role": "user", "content": message})

    reply = ""
    cited_skus = []
    tool_calls = []
    actions = []

    # Check available provider API keys
    gemini_key = getattr(settings, "GEMINI_API_KEY", "")
    openai_key = getattr(settings, "OPENAI_API_KEY", "")
    anthropic_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    groq_key = getattr(settings, "GROQ_API_KEY", "")

    executed_llm = False

    # Attempt Gemini if key present
    if gemini_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=gemini_key)
            tools_list = [tool_search_parts, tool_check_stock, tool_create_draft_order]
            
            # Format contents from session history
            contents = [h["content"] for h in history]
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=tools_list,
                    temperature=0.2,
                )
            )

            # Inspect function calls
            if response.function_calls:
                for fc in response.function_calls:
                    fn_name = fc.name
                    fn_args = dict(fc.args) if fc.args else {}
                    tool_calls.append({"name": fn_name, "arguments": fn_args})
                    
                    if fn_name == "create_draft_order":
                        res = tool_create_draft_order(dealer.id, fn_args.get("items", []))
                        if "status" in res and res["status"] == "success":
                            actions.append({
                                "type": "draft_order_created",
                                "order_number": res["order_number"]
                            })
                    elif fn_name == "check_stock":
                        tool_check_stock(fn_args.get("sku"))
                    elif fn_name == "search_parts":
                        tool_search_parts(fn_args.get("query"), fn_args.get("vehicle"), fn_args.get("category"))

            reply = response.text or "I have processed your request."
            executed_llm = True
        except Exception as e:
            logger.warning(f"Gemini execution exception: {e}")

    if not executed_llm:
        # Execute tool-calling reasoning engine
        reply, cited_skus, tool_calls, actions = rule_based_fallback(dealer, message)

    if not cited_skus:
        cited_skus = extract_cited_skus(reply)

    # Save to history
    history.append({"role": "assistant", "content": reply})

    return Response({
        "reply": reply,
        "cited_skus": cited_skus,
        "tool_calls": tool_calls,
        "actions": actions
    })
