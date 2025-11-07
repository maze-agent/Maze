// Site configuration
const SITE_CONFIG = {
    title: "Maze Framework",
    description: "The framework for distributed agents",
    subtitle: "Maze: multiple agent deploying and task level distribution",
    
    // Navigation links
    navLinks: {
        sourceCode: "https://github.com/QinbinLi/Maze",
        paper: "https://arxiv.org/",
        docs: "https://maze-doc.readthedocs.io/en/latest/index.html",
        aboutUs: "#"
    },
    
    // Demo videos
    demos: {
        workflowDesigner: {
            title: "🎯 Demo 1: Workflow Designer",
            description: "See how to visually design and orchestrate complex multi-agent workflows with our intuitive playground interface.",
            videoUrl: "https://meeting-agent1.oss-cn-beijing.aliyuncs.com/create_workflow.mp4"
        },
        distributedExecution: {
            title: "🚀 Demo 2: Distributed Execution", 
            description: "Watch agents collaboratively execute tasks across distributed workers with real-time monitoring and results.",
            videoUrl: "https://meeting-agent1.oss-cn-beijing.aliyuncs.com/check_result.mp4"
        }
    },
    
    // Features
    features: [
        {
            icon: "⚡",
            title: "Accelerate Agent Development",
            description: "Rapidly build, configure, and compose multiple agents using modular templates and visual IDE."
        },
        {
            icon: "🛡️", 
            title: "Reliable Agent Deployment",
            description: "Multi-agent collaborative execution with human feedback, complex task scheduling and control."
        },
        {
            icon: "📊",
            title: "Observability & Quality Enhancement", 
            description: "Real-time monitoring of agent execution, problem diagnosis, and continuous model behavior iteration."
        }
    ],
    
    // Cookbook examples
    cookbook: [
        {
            title: "🚀 Quick Start: Simple Two-Task Workflow",
            badge: "Interactive",
            description: "A minimal example showing how to create a workflow with two sequential tasks. Each task receives input, processes it with a timestamp, and passes the result to the next task.",
            structure: "Task 1 → Task 2",
            features: ["⚡ Sequential Execution", "🔗 Auto Dependencies", "📊 Resource Management"],
            hasTypewriter: true,
            codeFile: "simple_workflow.py"
        },
        {
            title: "💰 Financial Risk Assessment Workflow",
            badge: "Advanced",
            description: "Multi-dimensional risk assessment combining LLM analysis with parallel Monte Carlo simulations. This example demonstrates heterogeneous resource allocation with CPU-intensive risk calculations.",
            structure: "LLM Analysis → [Market Risk, Credit Risk, Liquidity Risk] → Report Generation",
            features: ["🧠 LLM + Monte Carlo", "⚡ Parallel Execution", "📈 Financial Analysis"],
            hasTypewriter: false,
            githubUrl: "https://github.com/QinbinLi/Maze/tree/main/examples/financial_risk_workflow"
        },
        {
            title: "🎨 Multimodal Creative Content Generator",
            badge: "Advanced",
            description: "Generate text, image, and audio content in parallel from a single description. Demonstrates GPU/CPU heterogeneous scheduling with HuggingFace models (GPT-2, Stable Diffusion, Bark TTS).",
            structure: "Input Processing → [Text Gen (CPU), Image Gen (GPU), Audio Gen (CPU)] → HTML Report",
            features: ["🎨 3 Modalities", "🖥️ GPU + CPU", "🤖 HuggingFace Models"],
            hasTypewriter: false,
            githubUrl: "https://github.com/QinbinLi/Maze/tree/main/examples/multimodel_create_workflow"
        }
    ],
    
    // Architecture components
    architecture: [
        {
            icon: "🧠",
            title: "MaLearn",
            description: "Component for training and enhancing language models to improve intelligent decision-making and learning capabilities."
        },
        {
            icon: "⚙️",
            title: "MaWorker", 
            description: "Executes various tasks and coordinates multiple agents to ensure efficient collaboration."
        },
        {
            icon: "📋",
            title: "MaRegister",
            description: "Registers and manages agents to ensure all agents in the system operate normally."
        },
        {
            icon: "🧭",
            title: "MaPath",
            description: "Defines agent paths and task flows to help agents execute multi-step tasks."
        }
    ],
    
    // Footer links
    footerLinks: {
        privacyPolicy: "#",
        termsOfService: "#"
    }
};

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SITE_CONFIG;
}



