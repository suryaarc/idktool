const http = require('http');
const vscode = require('vscode');
const { handlePickerRequest } = require('./pickerHandler');
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
                try {
                    const data = JSON.parse(body || '{}');
                    const promptText = data.prompt || data.text || '';
                    const targetUuid = data.uuid || data.chat_uuid || data.conversation_id || null;
                    const targetIdentifier = data.uuid || data.title || data.conversation || null;

                    if (!promptText) {
                        res.writeHead(400, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ ok: false, error: 'Empty prompt' }));
                        return;
                    }

                    // 0. Targeted send: identifier (uuid or title) resolves against
                    // trajectorySummaries and delivers straight through the backend
                    // RPC (SendUserCascadeMessage) - bypasses the UI/picker/focus
                    // entirely, so it works regardless of what's currently open.
                    if (targetIdentifier) {
                        try {
                            const match = lsClient.resolveConversation(targetIdentifier);
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
                        return;
                    }

                    // 1. Copy prompt to clipboard safely
                    try {
                        await vscode.env.clipboard.writeText(promptText);
                    } catch (clipErr) {}

                    // 2. Open chat view and inject query with target UUID if present
                    let handled = false;
                    let lastError = null;

                    const targets = [];
                    if (targetUuid) {
                        targets.push(
                            { cmd: 'antigravity.sendPromptToAgentPanel', arg: { prompt: promptText, conversationId: targetUuid, sessionId: targetUuid, uuid: targetUuid, id: targetUuid } },
                            { cmd: 'antigravity.sendPromptToAgentPanel', arg: { prompt: promptText, conversationId: targetUuid } },
                            { cmd: 'workbench.action.chat.open', arg: { query: promptText, conversationId: targetUuid, sessionId: targetUuid } }
                        );
                    }
                    targets.push(
                        { cmd: 'antigravity.sendPromptToAgentPanel', arg: promptText },
                        { cmd: 'workbench.action.chat.open', arg: { query: promptText } },
                        { cmd: 'workbench.action.chat.open', arg: promptText }
                    );

                    for (const t of targets) {
                        try {
                            await vscode.commands.executeCommand(t.cmd, t.arg);
                            handled = true;
                            break;
                        } catch (e) {
                            lastError = e.message;
                        }
                    }

                    // 3. If direct injection didn't handle it, open/focus the agent panel safely
                    if (!handled) {
                        const focusTargets = [
                            'antigravity.agentSidePanel.focus',
                            'antigravity.openAgent',
                            'antigravity.toggleChatFocus',
                            'antigravity.agentSidePanel.open',
                            'workbench.action.chat.open'
                        ];

                        for (const cmd of focusTargets) {
                            try {
                                await vscode.commands.executeCommand(cmd);
                                handled = true;
                                break;
                            } catch (e) {}
                        }
                    }

                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({
                        ok: true,
                        status: 'Prompt received and injected',
                        handled,
                        target_uuid: targetUuid
                    }));

                } catch (err) {
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ ok: false, error: err.message }));
                }
            });
        } else if (req.method === 'GET' && pathname === '/commands') {
            try {
                const allCmds = await vscode.commands.getCommands(true);
                const filtered = allCmds.filter(c => 
                    c.toLowerCase().includes('chat') || 
                    c.toLowerCase().includes('antigravity') || 
                    c.toLowerCase().includes('prompt') || 
                    c.toLowerCase().includes('agent') ||
                    c.toLowerCase().includes('conversation') ||
                    c.toLowerCase().includes('session') ||
                    c.toLowerCase().includes('switch') ||
                    c.toLowerCase().includes('load') ||
                    c.toLowerCase().includes('pick')
                );
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: true, commands: filtered }));
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
        } else if (req.method === 'POST' && (pathname === '/picker' || pathname === '/switch')) {
            await handlePickerRequest(req, res);
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
