/**
 * Typewriter Animation for Code Display
 * Simulates typing effect for code examples
 */

class TypewriterAnimation {
    constructor() {
        this.typewriters = [];
        this.isTyping = false;
    }

    /**
     * Initialize a typewriter for a specific code block
     * @param {number} index - The index of the code block
     * @param {string} code - The code to display
     * @param {number} speed - Typing speed in ms per character
     */
    init(index, code, speed = 35) {
        const element = document.getElementById(`typewriter-code-${index}`);
        if (!element) {
            console.error(`Typewriter element not found: typewriter-code-${index}`);
            return;
        }

        this.typewriters[index] = {
            element: element,
            code: code,
            speed: speed,
            currentIndex: 0,
            isActive: false,
            timeoutId: null
        };

        // Start typing automatically
        setTimeout(() => this.startTyping(index), 500);
    }

    /**
     * Start typing animation for a specific typewriter
     * @param {number} index - The typewriter index
     */
    startTyping(index) {
        const typewriter = this.typewriters[index];
        if (!typewriter || typewriter.isActive) return;

        typewriter.isActive = true;
        typewriter.currentIndex = 0;
        typewriter.element.textContent = '';
        
        this.typeNextCharacter(index);
    }

    /**
     * Type the next character
     * @param {number} index - The typewriter index
     */
    typeNextCharacter(index) {
        const typewriter = this.typewriters[index];
        if (!typewriter || !typewriter.isActive) return;

        const { code, currentIndex, element, speed } = typewriter;

        if (currentIndex < code.length) {
            // Add next character
            element.textContent = code.substring(0, currentIndex + 1);
            
            // Add cursor
            const cursor = document.createElement('span');
            cursor.className = 'typewriter-cursor';
            element.appendChild(cursor);

            typewriter.currentIndex++;

            // Variable speed for more natural typing
            let delay = speed;
            const char = code[currentIndex];
            
            // Slower at line breaks
            if (char === '\n') {
                delay = speed * 3;
            }
            // Slower at punctuation
            else if ([',', '.', ':', ';', ')', '}', ']'].includes(char)) {
                delay = speed * 2;
            }
            // Faster for spaces
            else if (char === ' ') {
                delay = speed * 0.5;
            }

            typewriter.timeoutId = setTimeout(() => this.typeNextCharacter(index), delay);
        } else {
            // Typing complete - remove cursor after a moment
            setTimeout(() => {
                const cursor = element.querySelector('.typewriter-cursor');
                if (cursor) cursor.remove();
                typewriter.isActive = false;
            }, 1000);
        }
    }

    /**
     * Stop typing animation
     * @param {number} index - The typewriter index
     */
    stopTyping(index) {
        const typewriter = this.typewriters[index];
        if (!typewriter) return;

        if (typewriter.timeoutId) {
            clearTimeout(typewriter.timeoutId);
            typewriter.timeoutId = null;
        }
        
        typewriter.isActive = false;
        
        // Remove cursor
        const cursor = typewriter.element.querySelector('.typewriter-cursor');
        if (cursor) cursor.remove();
    }

    /**
     * Replay typing animation
     * @param {number} index - The typewriter index
     */
    replay(index) {
        this.stopTyping(index);
        setTimeout(() => this.startTyping(index), 200);
    }

    /**
     * Show full code immediately
     * @param {number} index - The typewriter index
     */
    showFull(index) {
        const typewriter = this.typewriters[index];
        if (!typewriter) return;

        this.stopTyping(index);
        typewriter.element.textContent = typewriter.code;
    }
}

// Simple workflow example code
const SIMPLE_WORKFLOW_CODE = `from maze import MaClient, task

# Define Task 1: Add timestamp to input
@task(
    inputs=["task1_input"],
    outputs=["task1_output"],
    resources={"cpu": 1, "cpu_mem": 123, "gpu": 1, "gpu_mem": 123}
)
def task1(params):
    """Add timestamp to input text"""
    from datetime import datetime
    
    task_input = params.get("task1_input")
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str
    
    return {"task1_output": result}


# Define Task 2: Add timestamp and suffix
@task(
    inputs=["task2_input"],
    outputs=["task2_output"],
    resources={"cpu": 10, "cpu_mem": 123, "gpu": 0.8, "gpu_mem": 324}
)
def task2(params):
    """Add timestamp and suffix to input"""
    from datetime import datetime
    
    task_input = params.get("task2_input")
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    result = task_input + time_str + "===="
    
    return {"task2_output": result}


# Create and run workflow
if __name__ == "__main__":
    print("🚀 Starting Simple Workflow...")
    
    # Connect to Maze server
    client = MaClient("http://localhost:8000")
    workflow = client.create_workflow()
    
    # Add Task 1
    task1_result = workflow.add_task(
        task1,
        inputs={"task1_input": "hello"}
    )
    
    # Add Task 2 (depends on Task 1)
    task2_result = workflow.add_task(
        task2,
        inputs={"task2_input": task1_result.outputs["task1_output"]}
    )
    
    # Execute workflow and show results
    workflow.run()
    workflow.show_results()
    
    print(f"Final Output: {task2_result.outputs['task2_output']}");`;

// Global typewriter instances
let globalTypewriter = null;
let mainTypewriter = null;

/**
 * Initialize typewriter for main page
 */
function initMainTypewriter() {
    console.log('🎬 Initializing main page typewriter animation...');
    
    // Wait a bit to ensure the examples component is loaded
    setTimeout(() => {
        const element = document.getElementById('typewriter-code-main');
        if (element) {
            mainTypewriter = new TypewriterAnimation();
            mainTypewriter.init('main', SIMPLE_WORKFLOW_CODE, 35);
            console.log('✅ Main typewriter animation initialized');
        } else {
            console.warn('⚠️ Main typewriter element not found, retrying...');
            // Retry after a longer delay
            setTimeout(initMainTypewriter, 1000);
        }
    }, 300);
}

/**
 * Initialize typewriter when DOM is ready (for cookbook page)
 */
function initTypewriter() {
    console.log('🎬 Initializing typewriter animation...');
    
    // Wait a bit to ensure the cookbook component is loaded
    setTimeout(() => {
        const element = document.getElementById('typewriter-code-0');
        if (element) {
            globalTypewriter = new TypewriterAnimation();
            globalTypewriter.init(0, SIMPLE_WORKFLOW_CODE, 35);
            console.log('✅ Typewriter animation initialized');
        } else {
            console.warn('⚠️ Typewriter element not found, retrying...');
            // Retry after a longer delay
            setTimeout(initTypewriter, 1000);
        }
    }, 300);
}

/**
 * Replay typewriter animation
 * @param {string|number} index - The typewriter index (can be 'main' for main page)
 */
function replayTypewriter(index) {
    // For main page typewriter
    if (index === undefined || index === 'main') {
        if (mainTypewriter) {
            mainTypewriter.replay('main');
            return;
        }
    }
    
    // For cookbook page typewriter
    if (globalTypewriter) {
        globalTypewriter.replay(index || 0);
    } else {
        console.error('Typewriter not initialized');
    }
}

/**
 * Toggle code block expansion
 * @param {number} index - The code block index
 */
function toggleCode(index) {
    const codeBlock = document.getElementById(`code-block-${index}`);
    const button = event.target.closest('.code-expand-btn');
    
    if (!codeBlock || !button) return;
    
    if (codeBlock.classList.contains('collapsed')) {
        // Expand
        codeBlock.classList.remove('collapsed');
        button.classList.add('expanded');
    } else {
        // Collapse
        codeBlock.classList.add('collapsed');
        button.classList.remove('expanded');
    }
}

// Auto-initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTypewriter);
} else {
    initTypewriter();
}

// Also expose for manual initialization
window.initTypewriter = initTypewriter;
window.initMainTypewriter = initMainTypewriter;
window.replayTypewriter = replayTypewriter;
window.toggleCode = toggleCode;

