const express = require('express');
const path = require('path');
const app = express();
const PORT = 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Health check
app.get('/health', (req, res) => res.json({ status: 'ok', uptime: process.uptime() }));

// API: Get bot status
app.get('/api/status', (req, res) => {
    res.json({
        bot: 'TraderJoes#3230',
        mode: 'paper',
        agents: 8,
        commands: 30,
        platforms: ['Kalshi', 'Polymarket', 'Robinhood', 'Coinbase', 'Phemex'],
        uptime: process.uptime(),
    });
});

// API: Get recent alerts
app.get('/api/alerts', (req, res) => {
    res.json({ alerts: [], message: 'Alerts displayed in Discord' });
});

// Serve the dashboard
app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

app.listen(PORT, '0.0.0.0', () => {
    console.log(`OpenClaw Studio running on port ${PORT}`);
});
