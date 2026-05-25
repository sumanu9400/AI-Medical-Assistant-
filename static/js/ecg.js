/**
 * Real-time Medical ECG Waveform Simulation
 * Simulates a realistic P-QRS-T complex heart rhythm
 */
class ECGMonitor {
    constructor(canvasId, options = {}) {
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d');
        
        this.options = {
            color: options.color || '#00ffe5',
            lineWidth: options.lineWidth || 2,
            speed: options.speed || 2,
            bpm: options.bpm || 72,
            ...options
        };

        this.points = [];
        this.x = 0;
        this.resize();
        
        // Define the P-QRS-T waveform segments (normalized)
        // Each part of the heartbeat: [x_offset, y_amplitude]
        this.waveform = [
            [0, 0], [0.1, 0], [0.15, -0.1], [0.2, 0], [0.3, 0],  // P wave
            [0.35, 0.05], [0.4, -0.8], [0.45, 0.4], [0.5, 0],    // QRS complex
            [0.6, 0], [0.7, -0.15], [0.8, 0], [1.0, 0]           // T wave
        ];
        
        this.lastBeatTime = 0;
        this.beatInterval = 60000 / this.options.bpm;
        
        window.addEventListener('resize', () => this.resize());
        this.animate();
    }

    resize() {
        const parent = this.canvas.parentElement;
        this.width = parent.clientWidth;
        this.height = parent.clientHeight;
        this.canvas.width = this.width;
        this.canvas.height = this.height;
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
    }

    animate() {
        const now = Date.now();
        const deltaTime = now - (this.lastFrameTime || now);
        this.lastFrameTime = now;

        // Clear with slight fade for trail effect
        this.ctx.clearRect(0, 0, this.width, this.height);

        // Move current drawing position
        this.x += this.options.speed;
        if (this.x > this.width) {
            this.x = 0;
            this.points = []; // Reset on loop
        }

        // Calculate current value based on heart beat timing
        let value = 0;
        const timeSinceLastBeat = now - this.lastBeatTime;
        
        if (timeSinceLastBeat > this.beatInterval) {
            this.lastBeatTime = now;
            // Add a little randomness to BPM for realism
            this.beatInterval = (60000 / this.options.bpm) * (0.95 + Math.random() * 0.1);
        }

        const beatProgress = timeSinceLastBeat / 800; // Assume 800ms for one full pulse visualization
        if (beatProgress < 1) {
            // Interpolate waveform
            value = this.getWaveformValue(beatProgress);
        }

        // Add point
        const y = (this.height / 2) + (value * (this.height * 0.4));
        this.points.push({ x: this.x, y: y });

        // Limit points to prevent performance degradation
        if (this.points.length > 500) this.points.shift();

        // Draw the line
        this.ctx.beginPath();
        this.ctx.strokeStyle = this.options.color;
        this.ctx.lineWidth = this.options.lineWidth;
        this.ctx.shadowBlur = 10;
        this.ctx.shadowColor = this.options.color;

        for (let i = 0; i < this.points.length; i++) {
            const p = this.points[i];
            if (i === 0) this.ctx.moveTo(p.x, p.y);
            else {
                // If the line wraps around, don't draw the connecting segment
                if (p.x < this.points[i-1].x) {
                    this.ctx.stroke();
                    this.ctx.beginPath();
                    this.ctx.moveTo(p.x, p.y);
                } else {
                    this.ctx.lineTo(p.x, p.y);
                }
            }
        }
        this.ctx.stroke();

        // Draw "leading" dot
        this.ctx.beginPath();
        this.ctx.arc(this.x, y, 3, 0, Math.PI * 2);
        this.ctx.fillStyle = '#fff';
        this.ctx.fill();

        requestAnimationFrame(() => this.animate());
    }

    getWaveformValue(progress) {
        for (let i = 0; i < this.waveform.length - 1; i++) {
            const p1 = this.waveform[i];
            const p2 = this.waveform[i+1];
            if (progress >= p1[0] && progress <= p2[0]) {
                const t = (progress - p1[0]) / (p2[0] - p1[0]);
                return p1[1] + (p2[1] - p1[1]) * t;
            }
        }
        return 0;
    }
}

// Global initialization helper
function initECG(canvasId, options) {
    return new ECGMonitor(canvasId, options);
}
