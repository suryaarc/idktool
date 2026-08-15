const http = require('http');
const Module = require('module');

// 1. Intercept require('vscode') with a Mock implementation
const mockClipboard = { lastText: null };
const executedCommands = [];

const originalRequire = Module.prototype.require;
Module.prototype.require = function (path) {
    if (path === 'vscode') {
        return {
            env: {
                clipboard: {
                    writeText: async (text) => {
                        mockClipboard.lastText = text;
                    }
                }
            },
            commands: {
                executeCommand: async (cmd, arg) => {
                    executedCommands.push({ cmd, arg });
                    if (cmd.includes('fail')) throw new Error('Command failed');
                    return true;
                },
                getCommands: async (filter) => {
                    return [
                        'workbench.action.chat.open',
                        'antigravity.sendPrompt',
                        'antigravity.focusPrompt',
                        'antigravity.newChat',
                        'editor.action.clipboardPasteAction',
                        'workbench.action.chat.new',
                        'some.other.command'
                    ];
                },
                registerCommand: (cmd, callback) => {
                    return { dispose: () => {} };
                }
            },
            window: {
                setStatusBarMessage: () => {},
                showInformationMessage: () => {}
            }
        };
    }
    return originalRequire.apply(this, arguments);
};

// 2. Load extension
const extension = require('./extension.js');

function makeRequest(options, postData = null) {
    return new Promise((resolve, reject) => {
        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    resolve({ statusCode: res.statusCode, headers: res.headers, body: JSON.parse(data || '{}') });
                } catch (e) {
                    resolve({ statusCode: res.statusCode, headers: res.headers, rawBody: data });
                }
            });
        });
        req.on('error', reject);
        if (postData) {
            req.write(typeof postData === 'string' ? postData : JSON.stringify(postData));
        }
        req.end();
    });
}

const TEST_PORT = 9998;
process.env.BRIDGE_PORT = String(TEST_PORT);

async function runTests() {
    console.log('--- Starting Extension Bridge Tests ---');
    const mockContext = { subscriptions: [] };
    
    extension.activate(mockContext);
    await new Promise(r => setTimeout(r, 200));

    let failed = 0;
    let passed = 0;

    function assert(condition, message) {
        if (condition) {
            console.log(`[PASS] ${message}`);
            passed++;
        } else {
            console.error(`[FAIL] ${message}`);
            failed++;
        }
    }

    try {
        // Test 1: OPTIONS /send (CORS Preflight)
        const corsRes = await makeRequest({ host: '127.0.0.1', port: TEST_PORT, path: '/send', method: 'OPTIONS' });
        assert(corsRes.statusCode === 204, 'CORS OPTIONS returned 204');
        assert(corsRes.headers['access-control-allow-origin'] === '*', 'CORS Allow-Origin header present');

        // Test 2: GET /commands
        const cmdsRes = await makeRequest({ host: '127.0.0.1', port: TEST_PORT, path: '/commands', method: 'GET' });
        assert(cmdsRes.statusCode === 200, 'GET /commands returned 200');
        assert(cmdsRes.body.ok === true, 'GET /commands body ok is true');
        assert(Array.isArray(cmdsRes.body.commands) && cmdsRes.body.commands.includes('antigravity.sendPrompt'), 'Filtered commands contains antigravity.sendPrompt');

        // Test 3: POST /send valid prompt
        executedCommands.length = 0;
        const sendRes = await makeRequest({
            host: '127.0.0.1',
            port: TEST_PORT,
            path: '/send',
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }, { prompt: 'Test automated prompt injection' });
        
        assert(sendRes.statusCode === 200, 'POST /send returned 200');
        assert(sendRes.body.ok === true, 'POST /send body ok is true');
        assert(mockClipboard.lastText === 'Test automated prompt injection', 'Prompt written to clipboard');
        assert(executedCommands.some(c => c.cmd.includes('antigravity') || c.cmd.includes('chat')), 'Executed prompt injection command');

        // Test 4: POST /send empty prompt (Error handling)
        const emptyRes = await makeRequest({
            host: '127.0.0.1',
            port: TEST_PORT,
            path: '/send',
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }, { prompt: '' });
        assert(emptyRes.statusCode === 400, 'POST /send with empty prompt returned 400');
        assert(emptyRes.body.error === 'Empty prompt', 'Returns expected error message');

        // Test 5: POST /new_chat
        const newChatRes = await makeRequest({
            host: '127.0.0.1',
            port: TEST_PORT,
            path: '/new_chat',
            method: 'POST'
        });
        assert(newChatRes.statusCode === 200, 'POST /new_chat returned 200');
        assert(newChatRes.body.ok === true, 'POST /new_chat body ok is true');

        // Test 6: POST /picker
        const pickerRes = await makeRequest({
            host: '127.0.0.1',
            port: TEST_PORT,
            path: '/picker',
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }, { query: 'gitlab' });
        assert(pickerRes.statusCode === 200, 'POST /picker returned 200');
        assert(pickerRes.body.ok === true, 'POST /picker body ok is true');
        assert(pickerRes.body.query === 'gitlab', 'POST /picker received query text');

        // Test 7: 404 Route
        const notFoundRes = await makeRequest({ host: '127.0.0.1', port: TEST_PORT, path: '/invalid', method: 'GET' });
        assert(notFoundRes.statusCode === 404, 'GET /invalid returned 404');

    } catch (err) {
        console.error('Test execution error:', err);
        failed++;
    } finally {
        extension.deactivate();
        console.log(`\n--- Test Summary: ${passed} passed, ${failed} failed ---`);
        process.exit(failed > 0 ? 1 : 0);
    }
}

runTests();
