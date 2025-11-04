# Financial Risk Assessment Workflow - LanggraphClient

## 🎯 Overall Business Scenario

This is a **portfolio risk management system** that simulates the risk assessment process of banks or asset management companies.

**Actual Business Process:**
Imagine you are a risk management manager at an asset management company, and a client entrusts you with managing a 100 million yuan investment portfolio (60% stocks + 30% bonds + 10% cash). Before investing, you need to assess the various risks this portfolio may face and provide the client with a professional risk assessment report.

## 📊 Workflow Execution Flow

```
1. Investment Portfolio Description Input
   ↓
2. [LLM Intelligent Analysis] Extract Key Parameters
   ↓
3. Execute Three Risk Assessments in Parallel (simultaneously to save time)
   ├─ [Market Risk] Calculate Potential Market Losses
   ├─ [Credit Risk] Assess Default Risk
   └─ [Liquidity Risk] Test Liquidation Capability
   ↓
4. [Summary Report] Generate Comprehensive Risk Assessment
```

## 🔍 Detailed Function Description

### 1. llm_analysis_node - LLM Intelligent Analysis Node

**Business Purpose:** Acts like an experienced financial analyst, reading text descriptions of investment portfolios and automatically extracting key data.

**Input:**
```
"Mixed investment portfolio with a total scale of 100 million yuan,
containing 60% stocks, 30% bonds, 10% cash,
investment period of 1 year, with moderate risk preference."
```

**What LLM Does:**
- Understands this natural language description
- Extracts key parameters: total value, volatility, confidence level, time horizon, etc.
- Converts text into computable numerical parameters

**Output:**
```python
{
    "portfolio_value": 10000,      # 100 million yuan
    "volatility": 0.25,            # 25% volatility
    "confidence_level": 0.95,      # 95% confidence level
    "num_simulations": 500000,     # 500,000 simulations
    "time_horizon": 252            # 1 year (252 trading days)
}
```

**Why LLM is Needed?** 
In actual business, clients or business departments provide text descriptions, not structured data. LLM can automatically understand and extract this information, eliminating manual data entry work.

---

### 2. market_risk_var_tool - Market Risk Assessment Tool

**Business Purpose:** Answers "If the market declines, how much money will I lose in the worst case?"

**Algorithm Used:** Monte Carlo Simulation + VaR (Value at Risk) Calculation

**What It Does:**
1. **Simulate 500,000 Future Price Paths**
   - Each path represents a possible market trend
   - Uses Geometric Brownian Motion model (financial market standard model)
   - Considers volatility: 25% (daily price fluctuation of the market)

2. **Calculate VaR Value**
   - At 95% confidence level, find the worst 5% scenarios
   - The average loss in these 5% scenarios is the VaR

**Actual Business Meaning:**
```
Example Output:
"With 95% probability, the maximum loss within 1 year will not exceed 25 million yuan"
```
This tells the client: under normal circumstances (95% probability), the loss will not exceed this amount. However, there is still a 5% chance of extreme cases with greater losses.

---

### 3. credit_risk_monte_carlo_tool - Credit Risk Assessment Tool

**Business Purpose:** Answers "If bond issuers default, how much will I lose?"

**Business Background:** 
The investment portfolio contains 30% corporate bonds. Companies may go bankrupt and default, resulting in principal loss.

**What It Does:**
1. **Assume 100 Bond Issuers**
   - Each issuer has a 2% default probability
   - 60% of principal is lost upon default

2. **Simulate 500,000 Default Scenarios**
   - Each time randomly determines which companies default
   - Calculates total loss in each scenario

3. **Statistical Analysis**
   - Expected Loss: average loss
   - 95% CVaR: maximum loss at 95% probability
   - 99% CVaR: maximum loss at 99% probability

**Actual Business Meaning:**
```
Example Output:
"Expected Loss: 1.2 million yuan
Maximum Loss at 95% Confidence: 3.5 million yuan
Maximum Loss at 99% Confidence: 5.2 million yuan"
```

This helps clients understand: on average, the loss will be 1.2 million, but in extreme cases, it could exceed 5 million.

---

### 4. liquidity_risk_stress_test_tool - Liquidity Risk Assessment Tool

**Business Purpose:** Answers "If liquidation is suddenly needed, how much will be lost?"

**Business Background:**
Clients may suddenly need to redeem funds (e.g., urgent need for money). At this time, assets need to be sold quickly, but the market may not have enough buyers, forcing discounted sales.

**What It Does:**
1. **Simulate 1,000 Stress Scenarios**
   - Different redemption pressures (how much money clients want back)
   - Different market liquidity conditions (availability of buyers)

2. **Simulate 500 Times Per Scenario** (500,000 total)
   - Redemption rate: what percentage of money clients want back
   - Liquidation discount: how much discount is forced during urgent sales (5%-30%)
   - Cash reserves: how much cash is available to give clients directly

3. **Calculate Liquidity Loss**
   - If cash is insufficient, assets need to be sold
   - Discount loss when selling assets

**Actual Business Meaning:**
```
Example Output:
"Average Liquidity Loss: 1.8 million yuan
Maximum Liquidity Loss: 8.5 million yuan
95th Percentile: 4.2 million yuan"
```

This tells clients: if urgent redemption is needed, the average loss is 1.8 million, with a worst-case scenario of 8.5 million.

---

### 5. generate_report_node - Comprehensive Report Generation Node

**Business Purpose:** Integrates the three-dimensional risk assessment results into a complete report

**What It Does:**
- Collects evaluation results from the three previous tools
- Formats according to specifications
- Outputs a professional risk assessment report

**Output Example:**
```
====================================
Investment Portfolio Comprehensive Risk Assessment Report
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
2. **Parallel Risk Calculation** (3 Tools) → Multi-dimensional Assessment  
3. **Report Generation** → Professional Report for Clients

In real business, such a system can:
- Automatically process client investment intentions
- Quickly assess various risks
- Generate reports compliant with regulatory requirements
- Help investment managers make decisions

## Execution Process

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

## 2. Configure API Key

Set Tongyi Qianwen API Key:

```bash
export DASHSCOPE_API_KEY="your_api_key_here"
```

## 3. Start Maze
Start Maze Head (if you have multiple machines, you can start Maze workers)
```bash
mazea start --head 
```
## 4. Run main.py

```bash
python main.py
```
