# 📊 Auto Rater: Email Triage Classification Performance Report
Analyzed 2 test configurations.

## ⚙️ Operational Performance Summary Table
| Configuration Name | Total Time (s) | Total Emails | Avg Sec/Email | L1 Prompt Tokens | L1 Completion Tokens |
|---|---|---|---|---|---|
| experimental_flash_only | 0.12s | 20 | 0.006s | 2792 | 2893 |
| production_deepseek_pair | 55.38s | 20 | 2.769s | 2792 | 2893 |

## 🎯 Triage Decisions Breakdown
### 🔍 Configuration: `experimental_flash_only`
- **Total Scanned Envelopes**: 20
- **Level 0 Static Noise Intercepted**: 1 (5.0%)
- **Level 1 Low-Cost Low Importance Filtered**: 0 (0.0%)
- **Escalated Critical/Important Emails**: 19 (95.0%)

### 🔍 Configuration: `production_deepseek_pair`
- **Total Scanned Envelopes**: 20
- **Level 0 Static Noise Intercepted**: 1 (5.0%)
- **Level 1 Low-Cost Low Importance Filtered**: 0 (0.0%)
- **Escalated Critical/Important Emails**: 19 (95.0%)

## 📉 Benchmark Alignment Analytics (Relative to Baseline)
### 📊 `experimental_flash_only` alignment against `production_deepseek_pair`:
- **Relative Classification Accuracy**: 100.0%
- **Relative Precision**: 0.0%
- **Relative Recall**: 0.0%
- **Relative F1 Score Balance Metric**: 0.000
- **Confusion Matrix Counts**: [True Important: 0, False Important: 0, False Noise: 0, True Noise: 19]
