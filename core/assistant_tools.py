import re
from decimal import Decimal
from django.db.models import Q
from thefuzz import fuzz
from .models import Product, Dealer, Order, OrderItem
from .views import generate_order_number, calculate_order_totals

def tool_search_parts(query=None, vehicle=None, category=None):
    """
    Search product catalogue for matching parts using database indexing
    combined with fuzzy matching scoring for robust product retrieval.
    """
    qs = Product.objects.all()
    if vehicle:
        qs = qs.filter(vehicle_fitment__icontains=vehicle)
    if category:
        qs = qs.filter(category__icontains=category)

    items = list(qs)
    
    # If a query is provided, score items with fuzzy matching across sku, name, brand
    if query:
        q_clean = query.lower()
        scored_items = []
        for p in items:
            score = max(
                fuzz.token_set_ratio(q_clean, p.sku.lower()),
                fuzz.token_set_ratio(q_clean, p.name.lower()),
                fuzz.token_set_ratio(q_clean, p.brand.lower()),
                fuzz.partial_ratio(q_clean, p.vehicle_fitment.lower())
            )
            # direct substring match bonus
            if q_clean in p.name.lower() or q_clean in p.sku.lower() or q_clean in p.brand.lower():
                score += 50
            if score >= 40:
                scored_items.append((score, p))
        scored_items.sort(key=lambda x: x[0], reverse=True)
        items = [p for _, p in scored_items]

    results = []
    for p in items[:10]:
        results.append({
            "sku": p.sku,
            "name": p.name,
            "category": p.category,
            "brand": p.brand,
            "vehicle_fitment": p.vehicle_fitment,
            "price": f"{p.price:.2f}",
            "stock": p.stock,
            "description": p.description[:100]
        })
    return results

def tool_check_stock(sku):
    """
    Check stock and current price for a specific product SKU.
    """
    try:
        p = Product.objects.get(sku=sku)
        return {
            "sku": p.sku,
            "name": p.name,
            "price": f"{p.price:.2f}",
            "stock": p.stock,
            "vehicle_fitment": p.vehicle_fitment
        }
    except Product.DoesNotExist:
        return {"error": f"Product with SKU {sku} not found"}

def tool_create_draft_order(dealer_id, items):
    """
    Create a draft order for a dealer.
    items: list of dicts with 'sku' and 'quantity'
    """
    try:
        dealer = Dealer.objects.get(id=dealer_id)
    except Dealer.DoesNotExist:
        return {"error": f"Dealer with ID {dealer_id} not found"}

    if not items or not isinstance(items, list):
        return {"error": "Items list must be non-empty"}

    seen = set()
    validated_items = []
    for it in items:
        sku = it.get("sku")
        qty = it.get("quantity")
        if not sku or qty is None:
            return {"error": "Item missing sku or quantity"}
        try:
            qty = int(qty)
            if qty < 1:
                return {"error": "Quantity must be >= 1"}
        except Exception:
            return {"error": "Quantity must be integer"}
        if sku in seen:
            return {"error": f"Duplicate SKU {sku}"}
        seen.add(sku)
        try:
            prod = Product.objects.get(sku=sku)
        except Product.DoesNotExist:
            return {"error": f"Product {sku} not found"}
        validated_items.append({"product": prod, "quantity": qty})

    order_num = generate_order_number()
    items_calc, subtotal, tax_total, grand_total = calculate_order_totals(validated_items)
    order = Order.objects.create(
        order_number=order_num,
        dealer=dealer,
        status=Order.STATUS_DRAFT,
        subtotal=subtotal,
        tax_total=tax_total,
        grand_total=grand_total
    )
    for it in items_calc:
        OrderItem.objects.create(
            order=order,
            product=it["product"],
            sku=it["sku"],
            name=it["name"],
            quantity=it["quantity"],
            unit_price=it["unit_price"],
            line_subtotal=it["line_subtotal"],
            line_tax=it["line_tax"],
            line_total=it["line_total"]
        )

    return {
        "status": "success",
        "order_number": order.order_number,
        "dealer_id": dealer.id,
        "grand_total": f"{grand_total:.2f}",
        "items_count": len(items_calc)
    }
