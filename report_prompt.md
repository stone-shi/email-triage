# Comprehensive Model Evaluation Report Generator Prompt

Use the following prompt template to generate a unified, highly professional analytical report evaluating language model performance across both classification (Triage) and text generation (Summarization) standards.

---

## 📋 Reusable Prompt Template

Copy and paste the block below into your LLM interface, replacing the bracketed placeholders with the actual compiled markdown file contents:

```markdown
You are an expert AI Architect and Senior Data Scientist. Analyze the complete benchmarking results from both the automated summarization quality evaluations and the dual-standard triage alignment reports to produce a unified, highly polished executive evaluation document.

### Inputs Provided:
1. **Summarization Quality Report** (`auto_rater_summarizer_report.md`):
[PASTE CONTENTS OF auto_rater_summarizer_report.md HERE]

2. **Triage Classification Performance Report** (`auto_rater_triage_report.md`):
[PASTE CONTENTS OF auto_rater_triage_report.md HERE]

### Your Goal:
Generate a complete, comprehensive comparative analysis evaluating the performance of each language model setup across BOTH operational triage boundaries and high-fidelity executive summarization vectors.

### Required Structure & Formatting:
1. **Key Behavioral Takeaways**: Group the models into clear operational tiers (e.g., Flawless Replicators, Open-Weights Local Champions, Ultra-Fast Edge Pre-Filters, and Selective Production Filters). Highlight their specific strengths, weaknesses, speed/latency trade-offs, and token cost efficiency.
2. **Markdown Summary Tables**: You MUST include exactly three comprehensive summary tables at the end of the report:
   - **Table 1: Summarization Quality Evaluation**: Compare the average Accuracy (Factuality), Conciseness (Formatting), and Actionability (Utility) scores for each generator model based on the LLM-as-a-Judge ratings. Include a brief qualitative overview column.
   - **Table 2: Triage Alignment vs. Automated Baseline**: Present the full alignment metrics (Latency, Accuracy, Precision, Recall, F1 Score, TP, FP, FN, TN) for all models evaluated against the broad automated reference standard.
   - **Table 3: Triage Alignment vs. Curated Human Data**: Present the full alignment metrics (Accuracy, Precision, Recall, F1 Score, TP, FP, FN, TN) for all models evaluated against the curated Human Platinum gold standard answers.

Format the entire output in clean GitHub-style markdown, using rich visual alert banners, bolded labels, and clear horizontal dividers to maximize scanability and visual excellence. Do not truncate any tables.
```
