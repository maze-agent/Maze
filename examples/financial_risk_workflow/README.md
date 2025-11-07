# Financial Risk Assessment Workflow - LanggraphClient

## 🎯 Overall Business Scenario

This is a **Portfolio Risk Management System** that simulates the risk assessment process of banks or asset management companies.

**Real Business Process:**
Imagine you are a risk management manager at an asset management company, and a client has entrusted you to manage a 100 million yuan investment portfolio (60% stocks + 30% bonds + 10% cash). Before investing, you need to assess the various risks that this portfolio may face and provide the client with a professional risk assessment report.

## 📊 Workflow Execution Process

```
1. Portfolio Description Input
   ↓
2. [LLM Intelligent Analysis] Extract Key Parameters
   ↓
3. Parallel Execution of Three Risk Assessments (simultaneous execution, time-saving)
   ├─ [Market Risk] Calculate Potential Market Losses
   ├─ [Credit Risk] Assess Default Risk
   └─ [Liquidity Risk] Test Liquidity Capability
   ↓
4. [Consolidated Report] Generate Comprehensive Risk Assessment
```

## 🔍 Detailed Function of Each Component

### 1. llm_analysis_node - LLM Intelligent Analysis Node

**Business Purpose:** Use LLM to understand natural language descriptions and extract key parameters. Like an experienced financial analyst who reads textual descriptions of investment portfolios and automatically extracts key data.


**Why LLM is Needed:** 
In actual business, clients or business departments provide textual descriptions rather than structured data. LLM can automatically understand and extract information, eliminating manual data entry.

---

### 2. market_risk_var_tool - Market Risk Assessment Tool

**Business Purpose:** Answers the question "If the market declines, how much money will I lose in the worst-case scenario?"

**Algorithm Used:** Monte Carlo Simulation + VaR (Value at Risk) Calculation
 
 
---

### 3. credit_risk_monte_carlo_tool - Credit Risk Assessment Tool

**Business Purpose:** Answers the question "If bond issuers default, how much will I lose?"


**Algorithm Used:** Monte Carlo Simulation

---

### 4. liquidity_risk_stress_test_tool - Liquidity Risk Assessment Tool

**Business Purpose:** Answers the question "If I suddenly need to liquidate assets, how much will I lose?". Clients may suddenly need to redeem funds (for example, urgent need for money). In this case, assets need to be sold quickly, but the market may not have enough buyers, leading to forced selling at lower prices.

**Algorithm Used:** Monte Carlo Simulation

---

### 5. generate_report_node - Comprehensive Report Generation Node

**Business Purpose:** Integrate the risk assessment results from three dimensions into a complete report

**Specific Tasks:**
- Collect assessment results from the three tools above
- Format the layout
- Output a professional risk assessment report

**Output Example:**
```
====================================
Comprehensive Portfolio Risk Assessment Report
====================================

【Risk Dimension 1】Market Risk
- VaR Value: 25 million yuan
- Maximum Potential Loss Ratio: 25%

【Risk Dimension 2】Credit Risk
- Expected Loss: 1.2 million yuan
- 95% CVaR: 3.5 million yuan

【Risk Dimension 3】Liquidity Risk
- Average Liquidity Loss: 1.8 million yuan
- Maximum Liquidity Loss: 8.5 million yuan
====================================
```


## 🎓 Workflow Summary

This workflow simulates a **complete investment portfolio risk management process**:

1. **Natural Language Understanding** (LLM) → Extract Parameters
2. **Parallel Risk Calculation** (3 tools) → Multi-dimensional Assessment  
3. **Report Generation** → Professional Report for Clients

In real business scenarios, such a system can:
- Automatically process clients' investment intentions
- Quickly assess various types of risks
- Generate reports that comply with regulatory requirements
- Help investment managers make decisions

## 💻 Execution Process


### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

## 2. Configure API Key

Set Tongyi Qianwen API Key:


```bash
export DASHSCOPE_API_KEY="your_api_key_here"
```

## 3. Run the Workflow
### Naive langgraph
If you want to run naive langgraph, execute directly:
```python
python langgraph_naive.py
```

### Maze runtime as langgraph backend
Maze can serve as the runtime backend for langgraph, significantly improving langgraph e2e performance
```bash
maze start --head --port HEAD_PORT # Start Maze Head
python langgraph_maze.py
```
We conducted the test on a machine with the Intel(R) Xeon(R) Gold 5117 CPU operating at 2.00GHz. For the Naive langgraph, it took about 3 minutes. When using Maze as the backend to run langgraph, it took approximately 47 seconds, representing a reduction of about 4 times in e2e latency optimization.