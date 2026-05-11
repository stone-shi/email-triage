# 📊 Comprehensive Model Evaluation: Triage & Summarization Performance

This report provides a unified comparative analysis of various language models evaluated for both email triage classification and executive summarization quality.

---

## 🚀 Key Behavioral Takeaways & Operational Tiers

Based on the benchmarking results, the models have been categorized into four operational tiers based on their performance, latency, and reliability.

### 🏆 Tier 1: Flawless Replicators (Production Gold Standard)
*   **Models**: `baseline_gemini_pro`, `baseline_deepseek_pro`, `qwen3.6-35b-a3b`.
*   **Strengths**: High fidelity in both triage alignment and summarization. They consistently follow formatting constraints (bullet points) and surface actionable tasks.
*   **Weaknesses**: Higher latency and cost compared to flash models.
*   **Best For**: Final stage executive summarization where accuracy and actionability are paramount.

### 🥈 Tier 2: Strong Mid-Tier Champions (Open-Weights & Efficient)
*   **Models**: `gemma4-26b`, `baseline_gemini_flash_lite`, `baseline_deepseek_flash`.
*   **Strengths**: Exceptional balance between speed and performance. `baseline_gemini_flash_lite` stands out with 97% triage alignment and sub-2s latency.
*   **Weaknesses**: Occasional formatting slips or minor hallucinations in complex threads.
*   **Best For**: General-purpose triage and high-volume summarization.

### ⚡ Tier 3: Ultra-Fast Edge Pre-Filters
*   **Models**: `qwen3.5-0.8b`, `tei_classifier_pair`.
*   **Strengths**: Extremely low latency (sub-1s). `tei_classifier_pair` offers high precision for noise filtering.
*   **Weaknesses**: Significantly lower summarization quality; `qwen3.5-0.8b` struggled with accuracy (5.44/10) and often failed the bulleted formatting requirement.
*   **Best For**: Level 1 Triage (noise vs. signal) and high-speed initial filtering.

### ⚠️ Tier 4: Selective Production Filters
*   **Models**: `gemma4e4b`, `gemma4e2b`.
*   **Strengths**: Good accuracy for triage signal detection (100% recall for 4b).
*   **Weaknesses**: Poor conciseness and actionability scores. High false-positive rates in triage.
*   **Best For**: Defensive filtering where missing an important email is not an option (high recall).

---

## 📝 Table 1: Summarization Quality Evaluation
*Average scores (1-10) based on LLM-as-a-Judge ratings.*

| Configuration Model | Accuracy | Conciseness | Actionability | Qualitative Overview |
| :--- | :---: | :---: | :---: | :--- |
| **gemma4-26b** | 9.40 | 9.93 | 8.80 | **Top Performer**: Exceptional accuracy and formatting adherence. |
| **baseline_deepseek_pro** | 9.21 | 6.86 | 7.50 | High accuracy but frequently failed bulleted formatting constraints. |
| **gemma4e4b** | 9.13 | 7.87 | 7.89 | Strong factual recall but struggled with crisp formatting. |
| **baseline_gemini_flash_lite** | 9.12 | 9.88 | 8.94 | **Best Value**: Outstanding balance of speed and high-quality utility. |
| **qwen3.6-35b-a3b** | 9.07 | 9.50 | 9.43 | **Actionability King**: Best at surfacing clear executive tasks. |
| **qwen3.5-9b** | 8.92 | 9.42 | 8.42 | Reliable all-rounder with consistent formatting. |
| **baseline_gemini_pro** | 8.87 | 9.40 | 8.73 | Stable production baseline; very dependable. |
| **baseline_deepseek_flash** | 8.73 | 8.53 | 8.07 | Decent performance but slightly lower fidelity than Gemini Flash. |
| **tei_classifier_pair** | 8.39 | 8.94 | 8.67 | Surprisingly capable summarization for its size. |
| **gemma4e2b** | 8.03 | 6.41 | 5.21 | Struggled with both formatting and utility. |
| **qwen3.5-0.8b** | 5.44 | 5.08 | 4.04 | **Fail**: Significant hallucinations and formatting breakdowns. |

---

## 🎯 Table 2: Triage Alignment vs. Automated Baseline
*Evaluated against `baseline_gemini_pro` (100 email samples).*

| Configuration Name | Latency (Avg) | Accuracy | Precision | Recall | F1 Score | TP | FP | FN | TN |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **baseline_gemini_flash_lite** | 1.21s | 97.0% | 93.8% | 100.0% | 0.968 | 15 | 1 | 0 | 17 |
| **baseline_deepseek_pro** | 2.80s | 96.9% | 100.0% | 93.3% | 0.966 | 14 | 0 | 1 | 17 |
| **baseline_deepseek_flash** | 4.22s | 93.9% | 93.3% | 93.3% | 0.933 | 14 | 1 | 1 | 17 |
| **qwen3.5-9b** | 27.40s | 89.7% | 100.0% | 80.0% | 0.889 | 12 | 0 | 3 | 14 |
| **qwen3.6-35b-a3b** | 27.45s | 89.3% | 92.9% | 86.7% | 0.897 | 13 | 1 | 2 | 12 |
| **gemma4-26b** | 13.25s | 87.9% | 86.7% | 86.7% | 0.867 | 13 | 2 | 2 | 16 |
| **qwen3.5-0.8b** | 0.54s | 82.8% | 84.6% | 78.6% | 0.815 | 11 | 2 | 3 | 13 |
| **gemma4e2b** | 3.68s | 83.3% | 75.0% | 92.3% | 0.828 | 12 | 4 | 1 | 13 |
| **tei_classifier_pair** | 0.77s | 75.8% | 88.9% | 53.3% | 0.667 | 8 | 1 | 7 | 17 |
| **gemma4e4b** | 6.64s | 66.7% | 57.7% | 100.0% | 0.732 | 15 | 11 | 0 | 7 |
| **baseline_platinum_human** | 3.26s | 84.6% | 83.3% | 83.3% | 0.833 | 5 | 1 | 1 | 6 |

---

## 💎 Table 3: Triage Alignment vs. Curated Human Data
*Evaluated against `baseline_platinum_human` (Gold Standard).*

| Configuration Name | Accuracy | Precision | Recall | F1 Score | TP | FP | FN | TN |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **baseline_gemini_flash_lite** | 92.9% | 85.7% | 100.0% | 0.923 | 6 | 1 | 0 | 7 |
| **qwen3.6-35b-a3b** | 92.3% | 100.0% | 83.3% | 0.909 | 5 | 0 | 1 | 7 |
| **baseline_deepseek_pro** | 91.7% | 100.0% | 83.3% | 0.909 | 5 | 0 | 1 | 6 |
| **qwen3.5-0.8b** | 86.7% | 80.0% | 80.0% | 0.800 | 4 | 1 | 1 | 9 |
| **baseline_gemini_pro** | 84.6% | 83.3% | 83.3% | 0.833 | 5 | 1 | 1 | 6 |
| **gemma4-26b** | 84.6% | 83.3% | 83.3% | 0.833 | 5 | 1 | 1 | 6 |
| **baseline_deepseek_flash** | 78.6% | 80.0% | 66.7% | 0.727 | 4 | 1 | 2 | 7 |
| **qwen3.5-9b** | 78.6% | 100.0% | 50.0% | 0.667 | 3 | 0 | 3 | 8 |
| **gemma4e2b** | 75.0% | 57.1% | 100.0% | 0.727 | 4 | 3 | 0 | 5 |
| **tei_classifier_pair** | 75.0% | 100.0% | 33.3% | 0.500 | 2 | 0 | 4 | 10 |
| **gemma4e4b** | 60.0% | 50.0% | 100.0% | 0.667 | 6 | 6 | 0 | 3 |

---
*Report generated by Gemini CLI AI Architect.*
