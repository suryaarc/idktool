const http = require('http');
const vscode = require('vscode');
const lsClient = require('./lsClient');

let server = null;

function activate(context) {
    console.log('[Antigravity-Bridge] Extension activated. Starting HTTP server on port 9999...');

    server = http.createServer(async (req, res) => {
        // CORS Headers for Dashboard Web UI
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'POST, GET, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

        if (req.method === 'OPTIONS') {
            res.writeHead(204);
            res.end();
            return;
        }

        const pathname = (req.url || '/').split('?')[0].replace(/\/$/, '') || '/';

        if (req.method === 'POST' && (pathname === '/send' || pathname === '/prompt')) {
            let body = '';
            req.on('data', chunk => { body += chunk; });
            req.on('end', async () => {
                let data;
                try {
                    data = JSON.parse(body || '{}');
                } catch (e) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ ok: false, error: 'Invalid JSON body in request: ' + e.message }));
                    return;
                }

                try {
                    const promptText = data.prompt || data.text || '';
                    const targetIdentifier = data.uuid || data.title || data.conversation || null;

                    if (!promptText) {
                        res.writeHead(400, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ ok: false, error: 'Empty prompt' }));
                        return;
                    }

                    // Delivered straight through the backend RPC (SendUserCascadeMessage) -
                    // bypasses the UI/focus entirely, works regardless of what's open.
                    let match;
                    if (targetIdentifier) {
                        match = lsClient.resolveConversation(targetIdentifier);
                    } else {
                        const convs = lsClient.listConversations();
                        if (convs.length === 0) throw new Error('No active conversations found in state DB');
                        match = convs[0];
                    }

                    await lsClient.sendUserCascadeMessage(match.cascadeId, promptText);
                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({
                        ok: true,
                        status: 'Prompt sent via direct RPC (SendUserCascadeMessage)',
                        cascadeId: match.cascadeId,
                        title: match.title
                    }));
                } catch (err) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({
                        ok: false,
                        error: err.message,
                        candidates: err.candidates
                    }));
                }
            });
        } else if (req.method === 'GET' && pathname === '/conversations') {
            try {
                const list = lsClient.listConversations();
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: true, conversations: list }));
            } catch (e) {
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: false, error: e.message }));
            }
        } else if (req.method === 'POST' && (pathname === '/new_chat' || pathname === '/new')) {
            const newChatCmds = ['antigravity.startNewConversation', 'workbench.action.chat.new', 'antigravity.newChat'];
            for (const cmd of newChatCmds) {
                try {
                    await vscode.commands.executeCommand(cmd);
                    break;
                } catch (e) {}
            }
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ok: true, status: 'New conversation started' }));
        } else {
            res.writeHead(404, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Not Found' }));
        }
    });

    const PORT = process.env.BRIDGE_PORT ? parseInt(process.env.BRIDGE_PORT, 10) : 9999;
    
    server.on('error', (err) => {
        console.error('[Antigravity-Bridge] Error:', err.message);
        if (err.code === 'EADDRINUSE') {
            vscode.window.setStatusBarMessage(`⚠️ Antigravity Bridge Port ${PORT} occupied`, 5000);
        }
    });

    server.listen(PORT, '127.0.0.1', () => {
        console.log(`[Antigravity-Bridge] Server listening on http://127.0.0.1:${PORT}`);
        vscode.window.setStatusBarMessage(`🚀 Antigravity Bridge Active on port ${PORT}`, 5000);
    });

    let disposable = vscode.commands.registerCommand('antigravityBridge.startServer', () => {
        vscode.window.showInformationMessage('Antigravity Bridge HTTP Server is running on port 9999');
    });

    context.subscriptions.push(disposable);
    context.subscriptions.push({
        dispose: () => {
            if (server) server.close();
        }
    });
}

function deactivate() {
    if (server) {
        server.close();
    }
}

module.exports = {
    activate,
    deactivate
};
