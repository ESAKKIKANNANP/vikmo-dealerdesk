# VIKMO DealerDesk — Machine Learning & Architectural Decisions

## 1. Machine Learning Forecasting Methodology

### 1.1 Objective
Produce demand forecasts (`forecast_units`) for 40 automotive aftermarket SKUs specified in `data/lead_times.csv` across four target weeks:
- 2026-09-28
- 2026-10-05
- 2026-10-12
- 2026-10-19

Total forecast rows: exactly 160 rows.

### 1.2 Data Characteristics & Exploration
1. **Sales History**: 3,964 weekly records across 40 SKUs starting 2024-09-30 up to 2026-09-21.
2. **Promotions**: Known promotions flag in historical sales and forward planned calendar (`data/promo_calendar.csv`).
3. **Data Variations**:
   - Mature SKUs with 104 weeks of history exhibiting trend, seasonality, and promotional surges.
   - Recently launched SKUs (`BRK-1105`, `CHN-1006`) with only 6 weeks of historical observations.
   - Intermittent/lumpy demand curves with periods of zero weekly demand.
4. **Calendar Effects**: Festival calendar captures Diwali dates (Diwali 2024: Nov 1, 2025: Oct 21, 2026: Nov 8).

### 1.3 Validation Strategy (Preventing Data Leakage)
To adhere to strict zero-leakage standards, **chronological validation (backtesting)** was implemented:
- **Validation Period**: The final 4 observed historical weeks (2026-08-31 to 2026-09-21), exactly mirroring the 4-week forecast horizon.
- **Training Period**: All historical weeks prior to 2026-08-31.
- No future sales or out-of-sample data points were used during parameter estimation or feature calculations.

### 1.4 Baseline Comparison & Benchmark Results
We evaluated five distinct model paradigms on the chronological holdout across all 40 SKUs:

| Model | MAE | RMSE | Notes / Weaknesses |
|---|---|---|---|
| **Naive Last Observed (Persistence)** | 11.73 | 19.58 | Fails during promotional transitions and demand swings |
| **Moving Average (4-week)** | 8.77 | 15.01 | Smoothes volatility but lags behind promotion spikes |
| **Moving Average (12-week)** | 9.08 | 15.02 | Over-smoothes recent trend shifts |
| **Seasonal Lag (52-week)** | 12.28 | 19.73 | Severe penalty on recent items (<52 weeks) and shifting festivals |
| **Promo-Aware Decomposition Model** | **6.82** | **9.87** | **Best performance: isolates base trend and incorporates planned promotional uplift & festival ramp** |

### 1.5 Final Model Architecture
The **Promo-Aware Decomposition Model** was selected based on validation metrics:
1. **Deseasonalized Clean Baseline**: Derives a clean non-promotional baseline rate using recency-weighted moving averages over the recent 12 weeks, down-weighting historical promotional spikes.
2. **SKU-Specific Promotional Uplift**: Estimates empirical promotional lift factors bounded within [1.0, 2.5] per SKU from historical promo versus non-promo periods.
3. **Known Promotional Calendar Incorporation**: Seamlessly applies the estimated multiplier when `promo_flag == 1` in `data/promo_calendar.csv`.
4. **Pre-Festive Diwali Lift**: Calibrates pre-Diwali demand momentum during late October leading into the November 8 festival.
5. **Non-Negativity Constraint**: Bounded at $\ge 0.0$ to guarantee strictly positive physical unit forecasts.

---

## 2. Inventory & Safety Stock Optimization Formulas

The inventory reorder calculation strictly satisfies Section 10.2 of `API_CONTRACT.md`:
- **Lead Time Demand ($LTD$)**:
  $$\text{LTD} = \sum_{w=1}^{L} \text{forecast\_units}_w$$
  where $L$ is `lead_time_weeks` from `data/lead_times.csv`.
- **Sample Standard Deviation ($\sigma$)**:
  Calculated with Bessel's correction ($ddof=1$) on the most recent 12 historical weeks of `units_sold` for each SKU (or all available weeks if $<12$).
- **Safety Stock ($SS$)**:
  $$\text{SS} = \lceil 1.65 \times \sigma \times \sqrt{L} \rceil$$
  reflecting a 95% cycle service level under normal lead-time demand assumptions.
- **Reorder Point ($ROP$)**:
  $$\text{ROP} = \text{LTD} + \text{SS}$$
- **Reorder Quantity ($ROQ$)**:
  $$\text{ROQ} = \max(0, \lceil \text{ROP} - \text{current\_stock} \rceil)$$
  evaluated against the **live database stock** at request time.

---

## 3. Generative AI Dealer Assistant Architecture

### 3.1 Design Principles
1. **Function Tool Calling**: Uses structured tool calling rather than text parsing. Executes genuine backend Python methods:
   - `search_parts`: Filters the product catalogue by query/vehicle/category.
   - `check_stock`: Retrieves real-time stock and unit prices.
   - `create_draft_order`: Invokes the validated order creation engine.
2. **Provider Agnostic**: Configurable via environment variables (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROQ_API_KEY`).
3. **Session State Isolation**: Stateful conversation memory indexed by `session_id`.
4. **Guardrails**: Prompt injection defense, grounding responses exclusively on live database records, declining off-topic queries, and preventing unverified hallucinated SKUs or orders.

---

## 4. Database & Concurrency Architecture

1. **PostgreSQL & Decimal Accuracy**: All financial transactions use `DecimalField(max_digits=12, decimal_places=2)` with `ROUND_HALF_UP` tax rounding per line.
2. **Concurrency & Overselling Protection**:
   - Order confirmation uses `select_for_update()` in atomic database transactions to guarantee race-condition safety.
   - Inventory is checked and locked atomically; if any line item has insufficient stock, the transaction is cleanly aborted with HTTP 409 `INSUFFICIENT_STOCK`.
3. **Order Lifecycle**: Strict state machine enforcing `DRAFT -> CONFIRMED -> DISPATCHED -> DELIVERED` and `CANCELLED`.
4. **Idempotency**: Supports `Idempotency-Key` headers for conflict detection and safe network retries.
