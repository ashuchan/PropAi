# Full QC Report — canary 1ef1060 (112865 units)

## Field null/empty rate (all 29 fields)
| Field | Null/empty | % |
|---|---:|---:|
| `name` | 214 | 0.2% |
| `unit_id` | 423 | 0.4% |
| `unit_id_raw` | 1852 | 1.6% |
| `floor_plan_name` | 2448 | 2.2% |
| `floor_plan_id` | 113 | 0.1% |
| `beds` | 2094 | 1.9% |
| `baths` | 4489 | 4.0% |
| `area_sqft` | 214 | 0.2% |
| `rent_low` | 6401 | 5.7% |
| `rent_high` | 6587 | 5.8% |
| `available_date_raw` | 36438 | 32.3% |
| `available_date_post_fix` | 4802 | 4.3% |
| `move_in_date` | 112865 | 100.0% |
| `lease_term` | 111828 | 99.1% |
| `building` | 88336 | 78.3% |
| `floor` | 108247 | 95.9% |
| `date_captured` | 214 | 0.2% |
| `concession_text` | 112865 | 100.0% |
| `concession_text_clean` | 112865 | 100.0% |
| `_concession_quality` | 112865 | 100.0% |
| `concession_value` | 112865 | 100.0% |
| `concession_source` | 112865 | 100.0% |
| `offer_banner` | 112865 | 100.0% |
| `offer_type` | 112865 | 100.0% |
| `offer_target` | 112865 | 100.0% |
| `offer_value` | 112865 | 100.0% |
| `offer_conditions` | 112865 | 100.0% |

## Per-field invalid values
### `area_sqft`
- sentinel_-1: **3483** (3.09%)

### `rent_high`
- less_than_low: **53** (0.05%)

### `rent_per_sqft`
- under_0.3_usd: **16** (0.01%)
- over_20_usd: **2** (0.00%)

### `rent_spread`
- over_5k: **451** (0.40%)

## Cross-field anomalies
- **floor_plan_id with multiple distinct names**: 18
- **floor_plan_name with multiple distinct ids**: 6552
- **available_date_raw vs post_fix divergence**: 0 units (Phase-16 rewrite count)
- **duplicate unit_id within property**: 8609 dup rows across 603 props

## Concession-field empty rates (5 fields)
- `concession_text`: 112865 empty (100%)
- `concession_text_clean`: 112865 empty (100%)
- `concession_value`: 112865 empty (100%)

## Offer-field empty rates (5 fields)
- `offer_banner`: 112865 empty (100%)
- `offer_type`: 112865 empty (100%)
- `offer_target`: 112865 empty (100%)
- `offer_value`: 112865 empty (100%)