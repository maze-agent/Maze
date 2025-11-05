"""
Financial Risk Assessment Workflow - Using LangGraph

Business Scenario:
Multi-dimensional risk assessment for investment portfolios, including:
1. Market Risk Analysis (VaR calculation)
2. Credit Risk Assessment (Monte Carlo simulation)
3. Liquidity Risk Analysis (Stress testing)

Workflow:
1. LLM analyzes portfolio and extracts key parameters
2. Execute three compute-intensive risk assessment tools in parallel
3. Aggregate results and generate risk report
"""
import json
import os
from typing import TypedDict, Annotated, List
from operator import add
import numpy as np
from langchain_community.chat_models.tongyi import ChatTongyi
from langgraph.graph import StateGraph, START, END
from maze import LanggraphClient
client = LanggraphClient("localhost:8000")

os.environ["DASHSCOPE_API_KEY"] = os.getenv("DASHSCOPE_API_KEY")
model = ChatTongyi(streaming=True)

class GraphState(TypedDict):
    """Workflow state"""
    portfolio_description: str  # Portfolio description
    portfolio_params: dict  # Parameters extracted by LLM
    market_risk_result: str  # Market risk result
    credit_risk_result: str  # Credit risk result
    liquidity_risk_result: str  # Liquidity risk result
    risk_reports: Annotated[List[str], add]  # Accumulated risk reports
 
def llm_analysis_node(state: GraphState) :
    """
    Use LLM to analyze portfolio description and extract key parameters
    """
    print("\n=== LLM Analysis Node ===")
    portfolio_desc = state["portfolio_description"]
    
    prompt = f"""
    Please analyze the following portfolio description and extract key risk assessment parameters:
    
    Portfolio: {portfolio_desc}
    
    Return the following parameters in JSON format:
    1. portfolio_value: Total portfolio value (in 10,000 CNY)
    2. volatility: Volatility (between 0-1)
    3. confidence_level: Confidence level (between 0-1)
    4. num_simulations: Recommended number of simulations (integer)
    5. time_horizon: Time horizon (in days)
    
    Return JSON only, no other content.
    """
    
    # Invoke LLM
    response = model.invoke(prompt)
    llm_output = response.content
    json_output = json.loads(llm_output)
    print(f"Extracted parameters: {json_output}")
    
    return {
        "portfolio_params": json_output
    }

@client.task
def market_risk_var_tool(state: GraphState) :
    """
    Market Risk Tool: Calculate VaR (Value at Risk) - Monte Carlo method
    This is a compute-intensive task requiring massive simulations
    """
    print("\n=== Market Risk VaR Calculation (Monte Carlo Simulation) ===")
    params = state["portfolio_params"]
    
    portfolio_value = params["portfolio_value"]
    volatility = params["volatility"]
    confidence_level = params["confidence_level"]
    num_simulations = params["num_simulations"]
    time_horizon = params["time_horizon"]
    
    
    np.random.seed(42)
    returns = np.zeros(num_simulations)
    
    for i in range(num_simulations):
        daily_returns = np.random.normal(0, volatility / np.sqrt(252), time_horizon)
        cumulative_return = np.prod(1 + daily_returns) - 1
        returns[i] = cumulative_return
    
    # compute VaR
    var_percentile = (1 - confidence_level) * 100
    var_return = np.percentile(returns, var_percentile)
    var_amount = portfolio_value * abs(var_return)
    
    result = (
        f"Market Risk (VaR) Assessment Result:\n"
        f"- Confidence Level: {confidence_level * 100}%\n"
        f"- Time Horizon: {time_horizon} days\n"
        f"- VaR Value: {var_amount:.2f} 10k CNY\n"
        f"- Maximum Potential Loss: {abs(var_return) * 100:.2f}%\n"
        f"- Number of Simulations: {num_simulations}"
    )
    
    print(result)
    
    return {
        "market_risk_result": result,
        "risk_reports": [result]
    }

@client.task
def credit_risk_monte_carlo_tool(state: GraphState) :
    """
    Credit Risk Tool: Monte Carlo Simulation of Credit Losses
    Simulate multiple credit entities' default probabilities and loss distributions
    """
    print("\n=== Credit Risk Monte Carlo Simulation ===")
    params = state["portfolio_params"]
    
    num_simulations = params["num_simulations"]
    num_entities = 100  # Number of credit entities
    default_prob = 0.02  # 2% default probability
    loss_given_default = 0.6  # 60% loss given default
    exposure_per_entity = params["portfolio_value"] / num_entities
    
    print(f"Starting {num_simulations} credit loss scenarios simulation...")
    
    # Monte Carlo simulation of credit losses
    np.random.seed(43)
    portfolio_losses = np.zeros(num_simulations)
    
    for i in range(num_simulations):
        # Simulate default for each entity
        defaults = np.random.binomial(1, default_prob, num_entities)
        num_defaults = np.sum(defaults)
        
        # Calculate total loss for the scenario
        total_loss = num_defaults * exposure_per_entity * loss_given_default
        portfolio_losses[i] = total_loss
      
    # Calculate statistics
    expected_loss = np.mean(portfolio_losses)
    credit_var_95 = np.percentile(portfolio_losses, 95)
    credit_var_99 = np.percentile(portfolio_losses, 99)
    
    result = (
        f"Credit Risk Assessment Result:\n"
        f"- Number of Credit Entities: {num_entities}\n"
        f"- Expected Loss: {expected_loss:.2f} 10k CNY\n"
        f"- 95% CVaR: {credit_var_95:.2f} 10k CNY\n"
        f"- 99% CVaR: {credit_var_99:.2f} 10k CNY\n"
        f"- Default Probability: {default_prob * 100}%\n"
        f"- Number of Simulations: {num_simulations}"
    )
    
    print(result)
    
    return {
        "credit_risk_result": result,
        "risk_reports": [result]
    }

@client.task
def liquidity_risk_stress_test_tool(state: GraphState) :
    """
    Liquidity Risk Tool: Stress testing - Simulate asset liquidity under extreme market conditions
    Test liquidity adequacy through massive random scenarios
    """
    print("\n=== Liquidity Risk Stress Test ===")
    params = state["portfolio_params"]
    
    portfolio_value = params["portfolio_value"]
    num_simulations = params["num_simulations"]
    num_stress_scenarios = 1000  # 1000 stress scenarios
    
    print(f"Starting {num_simulations} liquidity stress tests...")
    
    # Simulate liquidity demand under different stress scenarios
    np.random.seed(44)
    liquidity_shortfalls = []
    
    for scenario in range(num_stress_scenarios):
        # Multiple simulations per scenario
        simulations_per_scenario = num_simulations // num_stress_scenarios
        
        for i in range(simulations_per_scenario):
            # Simulate liquidity demand (redemption pressure)
            redemption_rate = np.random.beta(2, 5)  # Beta distribution for redemption rate
            redemption_amount = portfolio_value * redemption_rate
            
            # Simulate asset liquidation discount
            liquidation_discount = np.random.uniform(0.05, 0.30)  # 5%-30% discount
            
            # Simulate available cash
            cash_ratio = np.random.uniform(0.05, 0.15)  # 5%-15% cash ratio
            available_cash = portfolio_value * cash_ratio
            
            # Calculate liquidity gap
            needed_liquidation = max(0, redemption_amount - available_cash)
            liquidation_loss = needed_liquidation * liquidation_discount
            
            liquidity_shortfalls.append(liquidation_loss)
        
        if (scenario + 1) % 200 == 0:
            print(f"Completed {scenario + 1} / {num_stress_scenarios} stress scenarios")
    
    # Statistical analysis
    liquidity_shortfalls = np.array(liquidity_shortfalls)
    avg_shortfall = np.mean(liquidity_shortfalls)
    max_shortfall = np.max(liquidity_shortfalls)
    shortfall_95 = np.percentile(liquidity_shortfalls, 95)
    shortfall_99 = np.percentile(liquidity_shortfalls, 99)
    
    result = (
        f"Liquidity Risk Stress Test Result:\n"
        f"- Number of Stress Scenarios: {num_stress_scenarios}\n"
        f"- Average Liquidity Loss: {avg_shortfall:.2f} 10k CNY\n"
        f"- Maximum Liquidity Loss: {max_shortfall:.2f} 10k CNY\n"
        f"- 95th Percentile: {shortfall_95:.2f} 10k CNY\n"
        f"- 99th Percentile: {shortfall_99:.2f} 10k CNY\n"
        f"- Total Simulations: {len(liquidity_shortfalls)}"
    )
    
    print(result)
    
    return {
        "liquidity_risk_result": result,
        "risk_reports": [result]
    }

def generate_report_node(state: GraphState) :
    """
    Aggregate all risk assessment results and generate comprehensive risk report
    """
    print("\n=== Generate Comprehensive Risk Report ===")
    
    reports = state.get("risk_reports", [])
    
    final_report = "\n" + "=" * 60 + "\n"
    final_report += "Portfolio Comprehensive Risk Assessment Report\n"
    final_report += "=" * 60 + "\n\n"
    final_report += f"Portfolio Description: {state['portfolio_description']}\n\n"
    
    for i, report in enumerate(reports, 1):
        final_report += f"\n【Risk Dimension {i}】\n"
        final_report += report + "\n"
    
    final_report += "\n" + "=" * 60 + "\n"
    final_report += "Risk Assessment Completed\n"
    final_report += "=" * 60 + "\n"
    
    print(final_report)
    
    return state

def create_workflow():
    """Create financial risk assessment workflow"""
    builder = StateGraph(GraphState)
    
    # Add nodes
    builder.add_node("llm_analysis", llm_analysis_node)
    builder.add_node("market_risk", market_risk_var_tool)
    builder.add_node("credit_risk", credit_risk_monte_carlo_tool)
    builder.add_node("liquidity_risk", liquidity_risk_stress_test_tool)
    builder.add_node("generate_report", generate_report_node)
    
    # Build edges
    builder.add_edge(START, "llm_analysis")
    
    # After LLM analysis, execute three risk assessment tools in parallel
    builder.add_edge("llm_analysis", "market_risk")
    builder.add_edge("llm_analysis", "credit_risk")
    builder.add_edge("llm_analysis", "liquidity_risk")
    
    # After all three tools complete, generate report
    builder.add_edge("market_risk", "generate_report")
    builder.add_edge("credit_risk", "generate_report")
    builder.add_edge("liquidity_risk", "generate_report")
    
    builder.add_edge("generate_report", END)
    
    return builder.compile()

def main():
    """Main function"""
    print("=" * 60)
    print("Financial Risk Assessment Workflow")
    print("=" * 60)
    
    # Create workflow
    workflow = create_workflow()
    
    # Define initial input
    initial_state = {
        "portfolio_description": (
            "Mixed portfolio with total size of 100 million CNY, "
            "consisting of 60% stocks (mainly CSI 300 constituents), "
            "30% bonds (AAA corporate bonds and government bonds), "
            "10% cash and money market instruments. "
            "Investment period of 1 year, moderate risk appetite."
            "I want to simulate 500000 times."
        ),
        "portfolio_params": {},
        "market_risk_result": "",
        "credit_risk_result": "",
        "liquidity_risk_result": "",
        "risk_reports": []
    }
    
    # Execute workflow
    print("\nStarting risk assessment workflow...\n")
    result = workflow.invoke(initial_state)
    
    print("\n" + "=" * 60)
    print("Workflow execution completed!")
    print("=" * 60)

if __name__ == "__main__":
    main()