const express = require('express');
const path = require('path');
const fs = require('fs');
const app = express();
const PORT = 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Health check
app.get('/health', (req, res) => res.json({ status: 'ok', uptime: process.uptime() }));

// API: Read live data from bot's persistent JSON files
const DATA_DIR = '/app/data';

app.get('/api/analytics', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'analytics.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ total_trades: 0, openai_cost_usd: 0, current_equity: 10000 });
    }
});

app.get('/api/signals', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'signal_history.json'), 'utf8'));
        res.json(data.slice(-50));  // last 50 signals
    } catch(e) {
        res.json([]);
    }
});

app.get('/api/memory', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'agent_memory.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ risk_rules: [], lessons: [], strategies: {} });
    }
});

app.get('/api/paper', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'paper_portfolio.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ cash: 10000, positions: [], trades: [] });
    }
});

app.get('/api/context', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'context_memory.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({});
    }
});

// Serve the dashboard
app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

app.listen(PORT, '0.0.0.0', () => {
    console.log(`OpenClaw Studio running on port ${PORT}`);
});
