const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const { DatabaseSync } = require('node:sqlite');

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LOGS_DIR = path.join(os.homedir(), 'AppData', 'Roaming', 'Antigravity IDE', 'logs');
const STATE_DB = path.join(os.homedir(), 'AppData', 'Roaming', 'Antigravity IDE', 'User', 'globalStorage', 'state.vscdb');
const SERVICE = 'exa.language_server_pb.LanguageServerService';

function getConnectionInfo() {
    const dirs = fs.readdirSync(LOGS_DIR)
        .map(name => ({ name, mtime: fs.statSync(path.join(LOGS_DIR, name)).mtimeMs }))
        .sort((a, b) => b.mtime - a.mtime);

    for (const { name } of dirs) {
        const logPath = path.join(LOGS_DIR, name, 'ls-main.log');
        if (!fs.existsSync(logPath)) continue;
        const text = fs.readFileSync(logPath, 'utf8');
        const tokenMatch = text.match(/--csrf_token (\S+)/);
        const portMatch = text.match(/listening on random port at (\d+) for HTTPS/);
        if (tokenMatch && portMatch) {
            return { token: tokenMatch[1], port: parseInt(portMatch[1], 10) };
        }
    }
    throw new Error('Could not find language server csrf token / port in any ls-main.log');
}

function rpcCall(method, body) {
    const { token, port } = getConnectionInfo();
    const payload = JSON.stringify(body);
    return new Promise((resolve, reject) => {
        const req = https.request({
            host: '127.0.0.1',
            port,
            path: `/${SERVICE}/${method}`,
            method: 'POST',
            rejectUnauthorized: false,
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload),
                'x-codeium-csrf-token': token
            }
        }, res => {
            let data = '';
            res.on('data', chunk => { data += chunk; });
            res.on('end', () => {
                let parsed;
                try { parsed = JSON.parse(data || '{}'); } catch (e) { parsed = { raw: data }; }
                if (res.statusCode !== 200) {
                    reject(new Error(`${method} -> HTTP ${res.statusCode}: ${parsed.message || data}`));
                    return;
                }
                resolve(parsed);
            });
        });
        req.on('error', reject);
        req.write(payload);
        req.end();
    });
}

// --- Schema-less protobuf reader ---------------------------------------
// Only returns raw {field, wire, value} tuples (value is a Buffer for
// wire type 2) - no auto-interpretation, since only three fixed field
// paths are needed here (see trajectorySummaries structure below).

function readVarint(buf, pos) {
    let result = 0, shift = 1, byte;
    do {
        if (pos >= buf.length) throw new Error('truncated varint');
        byte = buf[pos++];
        result += (byte & 0x7f) * shift;
        shift *= 128;
    } while (byte & 0x80);
    return [result, pos];
}

function decodeMessage(buf) {
    const fields = [];
    let pos = 0;
    const n = buf.length;
    try {
        while (pos < n) {
            let tag;
            [tag, pos] = readVarint(buf, pos);
            const fieldNo = tag >>> 3;
            const wire = tag & 7;
            if (fieldNo === 0) return null;
            if (wire === 0) {
                let val; [val, pos] = readVarint(buf, pos);
                fields.push({ field: fieldNo, wire: 'varint', value: val });
            } else if (wire === 1) {
                if (pos + 8 > n) return null;
                fields.push({ field: fieldNo, wire: 'fixed64', value: buf.subarray(pos, pos + 8) });
                pos += 8;
            } else if (wire === 5) {
                if (pos + 4 > n) return null;
                fields.push({ field: fieldNo, wire: 'fixed32', value: buf.subarray(pos, pos + 4) });
                pos += 4;
            } else if (wire === 2) {
                let length; [length, pos] = readVarint(buf, pos);
                if (length < 0 || pos + length > n) return null;
                fields.push({ field: fieldNo, wire: 'bytes', value: buf.subarray(pos, pos + length) });
                pos += length;
            } else {
                return null;
            }
        }
    } catch (e) {
        return null;
    }
    return fields;
}

function findField(fields, fieldNo) {
    return fields ? fields.find(f => f.field === fieldNo) : undefined;
}

// --- trajectorySummaries -------------------------------------------------
// antigravityUnifiedStateSync.trajectorySummaries in state.vscdb:
//   repeated field 1 = one entry per conversation
//     field 1 = uuid (utf8 string bytes)
//     field 2 = sub-message:
//       field 1 = ANOTHER base64-encoded protobuf (double-wrapped - the
//                 raw bytes here are the ASCII characters of a base64
//                 string, not the decoded bytes themselves)
//         field 1 = the real title (utf8 string bytes)

function listConversations() {
    const db = new DatabaseSync(STATE_DB, { readOnly: true });
    let raw;
    try {
        const row = db.prepare(
            "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'"
        ).get();
        raw = row && row.value;
    } finally {
        db.close();
    }
    if (!raw) return [];

    const top = decodeMessage(Buffer.from(raw, 'base64'));
    if (!top) return [];

    const results = [];
    for (const entry of top) {
        if (entry.field !== 1 || entry.wire !== 'bytes') continue;
        try {
            const entryFields = decodeMessage(entry.value);
            const uuidField = findField(entryFields, 1);
            const subField = findField(entryFields, 2);
            if (!uuidField || !subField) continue;

            const cascadeId = uuidField.value.toString('utf8');
            const subFields = decodeMessage(subField.value);
            const b64Field = findField(subFields, 1);
            if (!b64Field) continue;

            const innerBytes = Buffer.from(b64Field.value.toString('utf8'), 'base64');
            const innerFields = decodeMessage(innerBytes);
            const titleField = findField(innerFields, 1);
            if (!titleField) continue;

            results.push({ cascadeId, title: titleField.value.toString('utf8') });
        } catch (e) {
            // one malformed entry shouldn't break the whole list
        }
    }
    return results;
}

function resolveConversation(identifier) {
    const conversations = listConversations();

    if (UUID_RE.test(identifier)) {
        const match = conversations.find(c => c.cascadeId.toLowerCase() === identifier.toLowerCase());
        if (!match) {
            throw new Error(`No known conversation with uuid ${identifier}`);
        }
        return match;
    }

    const needle = identifier.toLowerCase();
    const matches = conversations.filter(c => c.title.toLowerCase().includes(needle));

    if (matches.length === 0) {
        throw new Error(`No conversation title matches "${identifier}"`);
    }
    if (matches.length > 1) {
        const err = new Error(`"${identifier}" matches ${matches.length} conversations, be more specific`);
        err.candidates = matches.map(m => ({ cascadeId: m.cascadeId, title: m.title }));
        throw err;
    }
    return matches[0];
}

// Real shape captured straight off the wire (TLS-decrypted via SSLKEYLOGFILE +
// Wireshark, decoded from a genuine SendUserCascadeMessage call the real UI
// made). The field is `cascadeConfig`, NOT `userConfig` - that one wrong key
// name is exactly why earlier attempts got a silent 200 {} but the agent
// still errored downstream (the config was never actually applied).
const DEFAULT_CASCADE_CONFIG = {
    plannerConfig: {
        conversational: { plannerMode: 'CONVERSATIONAL_PLANNER_MODE_DEFAULT', agenticMode: true },
        toolConfig: {
            runCommand: { autoCommandConfig: { autoExecutionPolicy: 'CASCADE_COMMANDS_AUTO_EXECUTION_EAGER' } },
            notifyUser: { artifactReviewMode: 'ARTIFACT_REVIEW_MODE_ALWAYS' },
            permissionConfig: { defaultGrants: { ask: ['read_url(*)'] } }
        },
        requestedModel: { model: 'MODEL_PLACEHOLDER_M72' },
        ephemeralMessagesConfig: { enabled: true },
        knowledgeConfig: { enabled: true }
    },
    conversationHistoryConfig: { enabled: true }
};

// Create a new conversation. Confirmed off the wire: StartCascade itself carries
// no cascadeConfig - just source/cascadeId/workspaceUris/requestedModel. cascadeId
// is generated CLIENT-SIDE (crypto.randomUUID), not returned by the server.
async function startCascade(workspaceUri) {
    const cascadeId = require('crypto').randomUUID();
    await rpcCall('StartCascade', {
        source: 'CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT',
        cascadeId,
        workspaceUris: [workspaceUri],
        requestedModel: 'MODEL_PLACEHOLDER_M72'
    });
    return cascadeId;
}

async function sendUserCascadeMessage(cascadeId, text) {
    return rpcCall('SendUserCascadeMessage', {
        cascadeId,
        items: [{ text }],
        cascadeConfig: DEFAULT_CASCADE_CONFIG
    });
}

module.exports = {
    listConversations,
    resolveConversation,
    startCascade,
    sendUserCascadeMessage,
    rpcCall,
    getConnectionInfo
};

