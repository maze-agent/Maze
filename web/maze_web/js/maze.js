// Maze animation functionality
class MazeAnimation {
    constructor() {
        this.mazeContainer = document.getElementById('mazeContainer');
        this.mazeWalker = document.getElementById('mazeWalker');
        this.particlesContainer = document.getElementById('particles');
        this.isWalkerMoving = true;
        this.moveWalkerTimeout = null;
        this.init();
    }

    init() {
        this.createFullScreenMaze();
        this.createParticles();
        this.startMazeWalking();
        this.bindResizeEvent();
    }

    createFullScreenMaze() {
        const screenWidth = window.innerWidth;
        const screenHeight = window.innerHeight;
        const centerX = screenWidth / 2;
        const centerY = screenHeight / 2;
        const scale = Math.max(screenWidth, screenHeight) / 800;
        
        const paths = this.generateMazePaths(centerX, centerY, scale);
        const walls = this.generateMazeWalls(screenWidth, screenHeight, scale);

        this.renderMazeElements(paths, walls);
    }

    generateMazePaths(centerX, centerY, scale) {
        const paths = [];
        const centerSize = CONFIG.MAZE.CENTER_SIZE * scale;
        const pathThickness = CONFIG.MAZE.PATH_THICKNESS * scale;
        const armLength = CONFIG.MAZE.ARM_LENGTH * scale;
        const diagonalLength = CONFIG.MAZE.DIAGONAL_LENGTH * scale;
        const innerSize = CONFIG.MAZE.INNER_SIZE * scale;

        // Center cross
        paths.push({x: centerX - centerSize/2, y: centerY - centerSize/2, width: centerSize, height: pathThickness});
        paths.push({x: centerX - centerSize/2, y: centerY - centerSize/2, width: pathThickness, height: centerSize});
        paths.push({x: centerX - centerSize/2, y: centerY + centerSize/2 - pathThickness, width: centerSize, height: pathThickness});
        paths.push({x: centerX + centerSize/2 - pathThickness, y: centerY - centerSize/2, width: pathThickness, height: centerSize});

        // Arms
        paths.push({x: centerX + centerSize/2, y: centerY - pathThickness/2, width: armLength, height: pathThickness});
        paths.push({x: centerX + centerSize/2 + armLength - pathThickness, y: centerY - centerSize/2, width: pathThickness, height: centerSize});
        paths.push({x: centerX - centerSize/2 - armLength, y: centerY - pathThickness/2, width: armLength, height: pathThickness});
        paths.push({x: centerX - centerSize/2 - armLength, y: centerY - centerSize/2, width: pathThickness, height: centerSize});
        paths.push({x: centerX - centerSize/2, y: centerY - centerSize/2 - armLength, width: centerSize, height: pathThickness});
        paths.push({x: centerX - pathThickness/2, y: centerY + centerSize/2, width: pathThickness, height: 188});
        paths.push({x: centerX - centerSize/2, y: centerY + centerSize/2 + armLength - pathThickness, width: centerSize, height: pathThickness});

        // Diagonal paths
        paths.push({x: centerX + centerSize/2 + 50 * scale, y: centerY - centerSize/2 - 50 * scale, width: pathThickness, height: diagonalLength});
        paths.push({x: centerX + centerSize/2 + 50 * scale, y: centerY - centerSize/2 - 50 * scale, width: diagonalLength, height: pathThickness});
        paths.push({x: centerX + centerSize/2 + 50 * scale, y: centerY + centerSize/2 - diagonalLength + 50 * scale, width: pathThickness, height: diagonalLength});
        paths.push({x: centerX + centerSize/2 + 50 * scale, y: centerY + centerSize/2 + 50 * scale - pathThickness, width: diagonalLength, height: pathThickness});
        paths.push({x: centerX - centerSize/2 - 50 * scale - diagonalLength, y: centerY - centerSize/2 - 50 * scale, width: diagonalLength, height: pathThickness});
        paths.push({x: centerX - centerSize/2 - 50 * scale - diagonalLength, y: centerY - centerSize/2 - 50 * scale, width: pathThickness, height: diagonalLength});
        paths.push({x: centerX - centerSize/2 - 50 * scale - diagonalLength, y: centerY + centerSize/2 + 50 * scale - pathThickness, width: diagonalLength, height: pathThickness});
        paths.push({x: centerX - centerSize/2 - 50 * scale - diagonalLength, y: centerY + centerSize/2 - diagonalLength + 50 * scale, width: pathThickness, height: diagonalLength});

        // Inner paths
        paths.push({x: centerX - innerSize/2, y: centerY - innerSize/2, width: innerSize, height: pathThickness});
        paths.push({x: centerX - innerSize/2, y: centerY + innerSize/2 - pathThickness, width: innerSize, height: pathThickness});
        paths.push({x: centerX - innerSize/2, y: centerY - innerSize/2, width: pathThickness, height: innerSize});
        paths.push({x: centerX + innerSize/2 - pathThickness, y: centerY - innerSize/2, width: pathThickness, height: innerSize});

        return paths;
    }

    generateMazeWalls(screenWidth, screenHeight, scale) {
        const wallThickness = CONFIG.MAZE.PATH_THICKNESS * scale;
        const wallMargin = CONFIG.MAZE.WALL_MARGIN * scale;
        
        return [
            {x: wallMargin, y: wallMargin, width: screenWidth - 2 * wallMargin, height: wallThickness},
            {x: wallMargin, y: wallMargin, width: wallThickness, height: screenHeight - 2 * wallMargin},
            {x: wallMargin, y: screenHeight - wallMargin - wallThickness, width: screenWidth - 2 * wallMargin, height: wallThickness},
            {x: screenWidth - wallMargin - wallThickness, y: wallMargin, width: wallThickness, height: screenHeight - 2 * wallMargin}
        ];
    }

    renderMazeElements(paths, walls) {
        // Clear existing maze elements
        const existingPaths = this.mazeContainer.querySelectorAll('.maze-path, .maze-wall');
        existingPaths.forEach(element => element.remove());

        // Add paths
        paths.forEach(path => {
            const pathElement = document.createElement('div');
            pathElement.className = 'maze-path';
            pathElement.style.left = path.x + 'px';
            pathElement.style.top = path.y + 'px';
            pathElement.style.width = path.width + 'px';
            pathElement.style.height = path.height + 'px';
            this.mazeContainer.appendChild(pathElement);
        });

        // Add walls
        walls.forEach(wall => {
            const wallElement = document.createElement('div');
            wallElement.className = 'maze-wall';
            wallElement.style.left = wall.x + 'px';
            wallElement.style.top = wall.y + 'px';
            wallElement.style.width = wall.width + 'px';
            wallElement.style.height = wall.height + 'px';
            this.mazeContainer.appendChild(wallElement);
        });
    }

    createParticles() {
        const particleCount = Math.min(CONFIG.PARTICLES.MAX_COUNT, Math.floor((window.innerWidth * window.innerHeight) / 20000));
        
        for (let i = 0; i < particleCount; i++) {
            const particle = document.createElement('div');
            particle.className = 'particle';
            particle.style.left = Math.random() * 100 + '%';
            particle.style.top = Math.random() * 100 + '%';
            particle.style.animationDelay = Math.random() * CONFIG.PARTICLES.ANIMATION_DELAY_MAX + 's';
            particle.style.animationDuration = (Math.random() * (CONFIG.PARTICLES.ANIMATION_DURATION_MAX - CONFIG.PARTICLES.ANIMATION_DURATION_MIN) + CONFIG.PARTICLES.ANIMATION_DURATION_MIN) + 's';
            this.particlesContainer.appendChild(particle);
        }
    }

    startMazeWalking() {
        const walkPath = this.generateWalkPath();
        let currentIndex = 0;

        const moveWalker = () => {
            if (!this.isWalkerMoving) return;
            if (currentIndex < walkPath.length) {
                const point = walkPath[currentIndex];
                this.mazeWalker.style.left = (point.x - CONFIG.MAZE.WALKER_SIZE/2) + 'px';
                this.mazeWalker.style.top = (point.y - CONFIG.MAZE.WALKER_SIZE/2) + 'px';
                currentIndex++;
                setTimeout(moveWalker, CONFIG.MAZE.MOVE_DELAY);
            } else {
                currentIndex = 0;
                setTimeout(moveWalker, CONFIG.MAZE.MOVE_DELAY);
            }
        };

        this.handleScroll();
        window.addEventListener('scroll', () => this.handleScroll());
        moveWalker();
    }

    generateWalkPath() {
        const screenWidth = window.innerWidth;
        const screenHeight = window.innerHeight;
        const centerX = screenWidth / 2;
        const centerY = screenHeight / 2;
        const scale = Math.max(screenWidth, screenHeight) / 800;

        return [
            {x: centerX, y: centerY},
            {x: centerX + 75 * scale, y: centerY},
            {x: centerX + 75 * scale, y: centerY - 75 * scale},
            {x: centerX, y: centerY - 75 * scale},
            {x: centerX - 75 * scale, y: centerY - 75 * scale},
            {x: centerX - 75 * scale, y: centerY},
            {x: centerX - 75 * scale, y: centerY + 75 * scale},
            {x: centerX, y: centerY + 75 * scale},
            {x: centerX + 75 * scale, y: centerY + 75 * scale},
            {x: centerX + 75 * scale, y: centerY},
            {x: centerX + 150 * scale, y: centerY},
            {x: centerX + 150 * scale, y: centerY - 75 * scale},
            {x: centerX + 200 * scale, y: centerY - 75 * scale},
            {x: centerX + 250 * scale, y: centerY - 75 * scale},
            {x: centerX + 250 * scale, y: centerY - 150 * scale},
            {x: centerX + 250 * scale, y: centerY + 75 * scale},
            {x: centerX + 200 * scale, y: centerY + 75 * scale},
            {x: centerX + 150 * scale, y: centerY + 75 * scale},
            {x: centerX + 75 * scale, y: centerY},
            {x: centerX, y: centerY},
            {x: centerX - 150 * scale, y: centerY},
            {x: centerX - 150 * scale, y: centerY - 75 * scale},
            {x: centerX - 200 * scale, y: centerY - 75 * scale},
            {x: centerX - 250 * scale, y: centerY - 75 * scale},
            {x: centerX - 250 * scale, y: centerY - 150 * scale},
            {x: centerX - 250 * scale, y: centerY + 75 * scale},
            {x: centerX - 200 * scale, y: centerY + 75 * scale},
            {x: centerX - 150 * scale, y: centerY + 75 * scale},
            {x: centerX - 75 * scale, y: centerY},
            {x: centerX, y: centerY},
            {x: centerX, y: centerY - 150 * scale},
            {x: centerX - 75 * scale, y: centerY - 150 * scale},
            {x: centerX - 150 * scale, y: centerY - 150 * scale},
            {x: centerX - 200 * scale, y: centerY - 150 * scale},
            {x: centerX - 200 * scale, y: centerY - 200 * scale},
            {x: centerX + 75 * scale, y: centerY - 150 * scale},
            {x: centerX + 150 * scale, y: centerY - 150 * scale},
            {x: centerX + 200 * scale, y: centerY - 150 * scale},
            {x: centerX + 200 * scale, y: centerY - 200 * scale},
            {x: centerX, y: centerY},
            {x: centerX, y: centerY + 150 * scale},
            {x: centerX - 75 * scale, y: centerY + 150 * scale},
            {x: centerX - 150 * scale, y: centerY + 150 * scale},
            {x: centerX - 200 * scale, y: centerY + 150 * scale},
            {x: centerX - 200 * scale, y: centerY + 200 * scale},
            {x: centerX + 75 * scale, y: centerY + 150 * scale},
            {x: centerX + 150 * scale, y: centerY + 150 * scale},
            {x: centerX + 200 * scale, y: centerY + 150 * scale},
            {x: centerX + 200 * scale, y: centerY + 200 * scale},
            {x: centerX, y: centerY}
        ];
    }

    handleScroll() {
        const scrollY = window.scrollY || window.pageYOffset;
        if (scrollY > CONFIG.SCROLL.STOP_THRESHOLD && this.isWalkerMoving) {
            this.isWalkerMoving = false;
            if (this.moveWalkerTimeout) {
                clearTimeout(this.moveWalkerTimeout);
                this.moveWalkerTimeout = null;
            }
        } else if (scrollY < CONFIG.SCROLL.START_THRESHOLD && !this.isWalkerMoving) {
            this.isWalkerMoving = true;
            this.startMazeWalking();
        }
    }

    bindResizeEvent() {
        window.addEventListener('resize', () => {
            // Clear existing maze elements
            while (this.mazeContainer.firstChild) {
                this.mazeContainer.removeChild(this.mazeContainer.firstChild);
            }
            this.mazeContainer.appendChild(this.mazeWalker);
            
            // Recreate maze and particles
            this.createFullScreenMaze();
            this.createParticles();
        });
    }
}

// Don't auto-initialize - let the component loader handle it
// The component loader will call: window.mazeAnimation = new MazeAnimation();


