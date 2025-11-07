// Demo Modal functionality
class DemoModal {
    constructor() {
        this.modal = document.getElementById('demoModal');
        this.closeBtn = document.getElementById('closeDemo');
        this.init();
    }

    init() {
        this.bindEvents();
    }

    bindEvents() {
        this.closeBtn.addEventListener('click', () => this.close());
        
        // Close modal when clicking outside
        this.modal.addEventListener('click', (e) => {
            if (e.target === this.modal) {
                this.close();
            }
        });

        // Close modal with Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.close();
            }
        });
    }

    open() {
        this.modal.classList.add('active');
        document.body.style.overflow = 'hidden';
    }

    close() {
        this.modal.classList.remove('active');
        document.body.style.overflow = 'auto';
        this.pauseAllVideos();
    }

    pauseAllVideos() {
        const videos = document.querySelectorAll('.demo-modal video');
        videos.forEach(video => video.pause());
    }
}

// Global function for backward compatibility
function openDemoModal() {
    if (window.demoModal) {
        window.demoModal.open();
    } else {
        // If modal is not initialized yet, try to initialize it
        if (typeof initializeModal === 'function') {
            initializeModal();
            if (window.demoModal) {
                window.demoModal.open();
            }
        }
    }
}

// Make function globally available
window.openDemoModal = openDemoModal;

// Don't auto-initialize - let the component loader handle it
// The component loader will call: window.demoModal = new DemoModal();


