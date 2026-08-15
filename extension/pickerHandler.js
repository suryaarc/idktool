const vscode = require('vscode');

/**
 * Opens the past conversations picker in Antigravity IDE.
 */
async function handlePickerRequest(req, res) {
    try {
        let body = '';
        if (req && typeof req.on === 'function') {
            await new Promise((resolve) => {
                req.on('data', chunk => { body += chunk; });
                req.on('end', resolve);
            });
        }
        let data = {};
        try {
            if (body) data = JSON.parse(body);
        } catch (e) {}

        const targets = [
            'antigravity.openConversationPicker',
            'conversationPicker.showConversationPicker',
            'antigravity.openConversationWorkspaceQuickPick',
            'workbench.view.chat.sessions',
            'workbench.action.chat.toggleChatViewSessions'
        ];

        let handled = false;
        let executedCmd = null;

        for (const cmd of targets) {
            try {
                await vscode.commands.executeCommand(cmd);
                handled = true;
                executedCmd = cmd;
                break;
            } catch (e) {}
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: handled, command: executedCmd, query: data.query || null }));
    } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: err.message }));
    }
}

module.exports = { handlePickerRequest };
