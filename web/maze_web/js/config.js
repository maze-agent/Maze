// Configuration constants
const CONFIG = {
    MAZE: {
        CENTER_SIZE: 150,
        PATH_THICKNESS: 25,
        ARM_LENGTH: 200,
        DIAGONAL_LENGTH: 80,
        INNER_SIZE: 80,
        WALL_MARGIN: 50,
        WALKER_SIZE: 16,
        MOVE_DELAY: 400
    },
    PARTICLES: {
        MAX_COUNT: 25,
        SIZE: 3,
        ANIMATION_DURATION_MIN: 20,
        ANIMATION_DURATION_MAX: 45,
        ANIMATION_DELAY_MAX: 12
    },
    SCROLL: {
        STOP_THRESHOLD: 50,
        START_THRESHOLD: 10
    }
};

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = CONFIG;
}



