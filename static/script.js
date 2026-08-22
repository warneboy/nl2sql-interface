// DOM Elements
const dbType = document.getElementById('db-type');
const host = document.getElementById('host');
const port = document.getElementById('port');
const initialDatabase = document.getElementById('initial-database');
const username = document.getElementById('username');
const password = document.getElementById('password');
const connectBtn = document.getElementById('connect-btn');
const disconnectBtn = document.getElementById('disconnect-btn');
const executeBtn = document.getElementById('execute-btn');
const clearBtn = document.getElementById('clear-btn');
const queryInput = document.getElementById('query-input');
const chatMessages = document.getElementById('chat-messages');
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
const currentDatabase = document.getElementById('current-database');
const chatCurrentDb = document.getElementById('chat-current-db');
const connectionMessage = document.getElementById('connection-message');
const databaseList = document.getElementById('database-list');
const databaseExplorer = document.querySelector('.database-explorer');

// State
let isConnected = false;
let isExecuting = false;
let currentDb = null;
let connectionDetails = null;

// Command history (terminal-style up/down arrows)
let queryHistory = [];
let historyIndex = -1; // -1 = at the "new input" position
let draftInput = '';

function persistQueryHistory() {
    try {
        localStorage.setItem('nl2sql_query_history', JSON.stringify(queryHistory));
    } catch (e) {}
}

function loadQueryHistory() {
    try {
        const saved = localStorage.getItem('nl2sql_query_history');
        if (saved) {
            const parsed = JSON.parse(saved);
            if (Array.isArray(parsed)) {
                queryHistory = parsed;
            }
        }
    } catch (e) {}
    historyIndex = -1;
}

// Helper function to escape HTML
function escapeHtml(text) {
    if (text === null || text === undefined) return 'NULL';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

// Event Listeners
connectBtn.addEventListener('click', handleConnect);
disconnectBtn.addEventListener('click', handleDisconnect);
executeBtn.addEventListener('click', handleExecute);
clearBtn.addEventListener('click', handleClear);
queryInput.addEventListener('keydown', handleKeyDown);
queryInput.addEventListener('input', function() {
    if (historyIndex !== -1 && queryInput.value !== queryHistory[historyIndex]) {
        historyIndex = -1;
        draftInput = queryInput.value;
    }
});

// Profile Dropdown
document.addEventListener('DOMContentLoaded', function() {
    const profileBtn = document.getElementById('profile-btn');
    const profileMenu = document.getElementById('profile-menu');
    
    if (profileBtn) {
        profileBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            profileMenu.classList.toggle('show');
        });
        
        document.addEventListener('click', function(e) {
            if (!profileBtn.contains(e.target) && !profileMenu.contains(e.target)) {
                profileMenu.classList.remove('show');
            }
        });
    }
    
    loadUserProfile();
});

// Load User Profile
async function loadUserProfile() {
    try {
        const token = localStorage.getItem('authToken');
        if (!token) return;
        
        const response = await fetch('/user-profile', {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });
        
        const data = await response.json();
        
        if (response.ok) {
            document.getElementById('user-email-display').textContent = data.email;
            document.getElementById('profile-name').textContent = data.full_name;
            document.getElementById('profile-email').textContent = data.email;

            // Google accounts can't change password -> hide that option.
            const changePasswordBtn = document.getElementById('change-password-btn');
            if (changePasswordBtn) {
                changePasswordBtn.style.display = data.is_google_account ? 'none' : 'flex';
            }
        }
    } catch (error) {
        console.error('Error loading profile:', error);
    }
}

// Change Password
function showChangePassword() {
    document.getElementById('password-modal').style.display = 'flex';
    document.getElementById('current-password').value = '';
    document.getElementById('new-password').value = '';
    document.getElementById('confirm-password').value = '';
    document.getElementById('password-message').className = 'message';
    document.getElementById('password-message').textContent = '';
}

function closePasswordModal() {
    document.getElementById('password-modal').style.display = 'none';
}

async function submitPasswordChange() {
    const currentPassword = document.getElementById('current-password').value;
    const newPassword = document.getElementById('new-password').value;
    const confirmPassword = document.getElementById('confirm-password').value;
    const messageEl = document.getElementById('password-message');
    
    if (!currentPassword || !newPassword || !confirmPassword) {
        messageEl.textContent = 'All fields are required';
        messageEl.className = 'message error';
        messageEl.style.display = 'block';
        return;
    }
    
    if (newPassword.length < 8) {
        messageEl.textContent = 'New password must be at least 8 characters';
        messageEl.className = 'message error';
        messageEl.style.display = 'block';
        return;
    }
    
    if (newPassword !== confirmPassword) {
        messageEl.textContent = 'Passwords do not match';
        messageEl.className = 'message error';
        messageEl.style.display = 'block';
        return;
    }
    
    try {
        const token = localStorage.getItem('authToken');
        const response = await fetch('/change-password', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({
                current_password: currentPassword,
                new_password: newPassword
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            messageEl.textContent = data.message;
            messageEl.className = 'message success';
            messageEl.style.display = 'block';
            setTimeout(closePasswordModal, 1500);
        } else {
            messageEl.textContent = data.message || 'Password change failed';
            messageEl.className = 'message error';
            messageEl.style.display = 'block';
        }
    } catch (error) {
        messageEl.textContent = 'Error: ' + error.message;
        messageEl.className = 'message error';
        messageEl.style.display = 'block';
    }
}

// Logout - delete all stored info and close the database connection
async function handleLogout() {
    try {
        try {
            await fetch('/disconnect', { method: 'POST' });
        } catch (e) {}
        await fetch('/logout', { method: 'POST' });
    } catch (error) {
        console.error('Logout error:', error);
    } finally {
        localStorage.removeItem('authToken');
        localStorage.removeItem('user');
        localStorage.removeItem('nl2sql_query_history');
        sessionStorage.clear();
        window.location.href = '/';
    }
}

// Functions
async function handleConnect() {
    const connectionData = {
        db_type: dbType.value,
        host: host.value.trim(),
        port: port.value.trim(),
        database: initialDatabase.value.trim(),
        username: username.value.trim(),
        password: password.value.trim()
    };

    if (!connectionData.host || !connectionData.username) {
        showMessage('Please fill in Host and Username.', 'error');
        return;
    }

    if (!connectionData.port) {
        connectionData.port = connectionData.db_type === 'mysql' ? '3306' : '5432';
    }

    try {
        connectBtn.disabled = true;
        connectBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Connecting...';
        clearMessage();

        const response = await fetch('/connect', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(connectionData)
        });

        const data = await response.json();

        if (data.success) {
            isConnected = true;
            currentDb = data.database || 'None';
            connectionDetails = connectionData;
            updateUIAfterConnect(data);
            showMessage(data.message, 'success');
            await loadDatabases();
            addSystemMessage(' Connected to ' + data.db_type + ' server');
            clearMessage();
            
            // Load tables and auto-scroll
            await refreshTables(true); // Pass true to scroll after loading
            
        } else {
            showMessage(data.message, 'error');
        }
    } catch (error) {
        showMessage('Connection error: ' + error.message, 'error');
    } finally {
        connectBtn.disabled = false;
        connectBtn.innerHTML = '<i class="fas fa-link"></i> Connect';
    }
}


async function handleDisconnect() {
    try {
        const response = await fetch('/disconnect', {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            isConnected = false;
            currentDb = null;
            connectionDetails = null;
            updateUIAfterDisconnect();
            addSystemMessage(' Disconnected from server');
            clearMessage();
            document.getElementById('table-viewer').style.display = 'none';
        }
    } catch (error) {
        console.error('Disconnect error:', error);
    }
}

async function handleExecute() {
    const query = queryInput.value.trim();
    
    if (!query) {
        showMessage('Please enter a SQL query.', 'error');
        return;
    }

    addToHistory(query);

    if (isExecuting) return;

    try {
        isExecuting = true;
        executeBtn.disabled = true;
        executeBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Executing...';
        clearMessage();

        addUserMessage(query);

        const response = await fetch('/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ query: query })
        });

        const data = await response.json();

        if (data.success) {
            if (data.generated_sql) {
                const tag = data.fallback ? ' (fallback)' : '';
                addSystemMessage(' Generated SQL: ' + data.generated_sql + tag);
            }
            if (data.result) {
                addResultMessage(data.result);
                if (query.trim().toUpperCase().startsWith('SHOW DATABASES')) {
                    await loadDatabases();
                }
                if (query.trim().toUpperCase().startsWith('USE')) {
                    const dbName = query.trim().split(' ')[1].replace(';', '');
                    currentDb = dbName;
                    updateCurrentDatabaseDisplay(dbName);
                    await loadDatabases();
                    setTimeout(refreshTables, 500);
                }
                if (query.trim().toUpperCase().includes('SHOW TABLES') || 
                    query.trim().toUpperCase().includes('CREATE TABLE') || 
                    query.trim().toUpperCase().includes('DROP TABLE') || 
                    query.trim().toUpperCase().includes('ALTER TABLE')) {
                    setTimeout(refreshTables, 500);
                }
            } else {
                addErrorMessage('No data returned from query');
            }
            queryInput.value = '';
        } else {
            if (data.not_found) {
                addTableNotFoundMessage(data.error || 'This table does not exist in this database.');
            } else if (data.clarification) {
                addClarificationMessage(data.error || 'Please clarify your request.');
            } else {
                addErrorMessage(data.error || 'Query execution failed');
            }
        }
    } catch (error) {
        addErrorMessage('Execution error: ' + error.message + (error.message === 'Failed to fetch' ? ' (check that the server is running and try again)' : ''));
    } finally {
        isExecuting = false;
        executeBtn.disabled = false;
        executeBtn.innerHTML = '<i class="fas fa-play"></i> Execute';
        queryInput.focus();
    }
}

function handleClear() {
    queryInput.value = '';
    queryInput.focus();
    clearMessage();
}

function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!executeBtn.disabled && isConnected) {
            handleExecute();
        }
        return;
    }
    if (e.key === 'Enter' && e.shiftKey) {
        return;
    }
    if (e.key === 'ArrowUp') {
        handleArrowUp(e);
        return;
    }
    if (e.key === 'ArrowDown') {
        handleArrowDown(e);
        return;
    }
}

function getCaretLine() {
    const pos = queryInput.selectionStart;
    return queryInput.value.substring(0, pos).split('\n').length - 1;
}

function getLineCount() {
    return queryInput.value.split('\n').length;
}

function setCaretToEnd() {
    queryInput.focus();
    queryInput.setSelectionRange(queryInput.value.length, queryInput.value.length);
}

function addToHistory(query) {
    query = query.trim();
    if (!query) return;
    if (queryHistory.length === 0 || queryHistory[queryHistory.length - 1] !== query) {
        queryHistory.push(query);
    }
    historyIndex = -1;
    persistQueryHistory();
}

function handleArrowUp(e) {
    if (queryHistory.length === 0) return;
    if (getLineCount() > 1 && getCaretLine() > 0) return; // let cursor move in multiline text
    e.preventDefault();
    if (historyIndex === -1) {
        draftInput = queryInput.value;
        const last = queryHistory.length - 1;
        // If the current input is already the most recent entry (e.g. the
        // query was just executed), jump straight to the one before it so the
        // very first arrow press already shows an older query.
        if (queryHistory.length > 1 && queryInput.value.trim() === queryHistory[last]) {
            historyIndex = last - 1;
        } else {
            historyIndex = last;
        }
    } else if (historyIndex > 0) {
        historyIndex--;
    }
    queryInput.value = queryHistory[historyIndex];
    setCaretToEnd();
}

function handleArrowDown(e) {
    if (historyIndex === -1) return; // nothing to navigate down from yet
    if (getLineCount() > 1 && getCaretLine() < getLineCount() - 1) return; // let cursor move in multiline text
    e.preventDefault();
    if (historyIndex < queryHistory.length - 1) {
        historyIndex++;
        queryInput.value = queryHistory[historyIndex];
    } else {
        historyIndex = -1;
        queryInput.value = draftInput;
    }
    setCaretToEnd();
}

async function loadDatabases() {
    try {
        const response = await fetch('/get-databases');
        const data = await response.json();
        
        if (data.success && data.databases) {
            displayDatabases(data.databases, data.current);
            databaseExplorer.style.display = 'block';
        }
    } catch (error) {
        console.error('Database load error:', error);
    }
}

function displayDatabases(databases, current) {
    let html = '';
    databases.sort().forEach(db => {
        const isActive = db === current;
        html += `<div class="db-item ${isActive ? 'active' : ''}" onclick="switchDatabase('${db}')">
            <span class="db-name"><i class="fas fa-database"></i> ${escapeHtml(db)}</span>
            ${isActive ? '<span class="db-badge">Active</span>' : ''}
        </div>`;
    });
    
    databaseList.innerHTML = html || '<p style="color: var(--text-muted);">No databases found.</p>';
}

window.switchDatabase = async function(dbName) {
    if (!isConnected) return;
    try {
        const response = await fetch('/use-database', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ database: dbName })
        });
        const data = await response.json();

        if (data.success) {
            currentDb = data.database || dbName;
            updateCurrentDatabaseDisplay(currentDb);
            addSystemMessage(' Switched to database: ' + currentDb);
            queryInput.value = '';
            setTimeout(() => { refreshTables(true); }, 300);
        } else {
            addErrorMessage(data.error || 'Could not switch to database: ' + dbName);
        }
    } catch (error) {
        // Transient network failure (e.g. server restarting) - try once more
        // before giving up with a clear message.
        try {
            const retry = await fetch('/use-database', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ database: dbName })
            });
            const data = await retry.json();
            if (data.success) {
                currentDb = data.database || dbName;
                updateCurrentDatabaseDisplay(currentDb);
                addSystemMessage(' Switched to database: ' + currentDb);
                queryInput.value = '';
                setTimeout(() => { refreshTables(true); }, 300);
                return;
            }
            addErrorMessage(data.error || 'Could not switch to database: ' + dbName);
        } catch (retryError) {
            addErrorMessage('Could not switch database (check that the server is still running): ' + retryError.message);
        }
    }
};

// Table Viewer Functions
async function refreshTables(scrollToView = false) {
    if (!isConnected) {
        return;
    }
    
    const tableViewer = document.getElementById('table-viewer');
    const content = document.getElementById('table-viewer-content');
    const dbName = document.getElementById('table-viewer-db');
    
    tableViewer.style.display = 'flex';
    dbName.textContent = currentDb || 'Current Database';
    content.innerHTML = '<div class="loading-tables"><i class="fas fa-spinner fa-spin"></i> Loading tables...</div>';
    
    try {
        const response = await fetch('/get-tables-with-data');
        const data = await response.json();
        
        if (data.success) {
            displayTables(data.tables);
            
            // Scroll if requested
            if (scrollToView) {
                scrollToTableViewer();
            }
        } else {
            content.innerHTML = '<div class="loading-tables">Error: ' + data.error + '</div>';
        }
    } catch (error) {
        content.innerHTML = '<div class="loading-tables">Error loading tables: ' + error.message + '</div>';
    }
}

function displayTables(tables) {
    const content = document.getElementById('table-viewer-content');
    const tableNames = Object.keys(tables);
    
    if (tableNames.length === 0) {
        content.innerHTML = '<div class="loading-tables">No tables found in this database</div>';
        return;
    }
    
    let html = '<div class="table-grid">';
    
    tableNames.forEach(tableName => {
        const table = tables[tableName];
        const columnCount = table.columns ? table.columns.length : 0;
        const rowCount = table.row_count || 0;
        
        html += `
            <div class="table-card" onclick="showTableDetail('${tableName}')">
                <div class="table-name">
                    <i class="fas fa-table"></i>
                    ${escapeHtml(tableName)}
                </div>
                <div class="table-info">
                    ${columnCount} columns • ${rowCount} rows
                </div>
            </div>
        `;
    });
    
    html += '</div>';
    html += '<div id="table-detail-container"></div>';
    content.innerHTML = html;
}

function showTableDetail(tableName) {
    const container = document.getElementById('table-detail-container');
    container.innerHTML = '<div class="loading-tables"><i class="fas fa-spinner fa-spin"></i> Loading ' + escapeHtml(tableName) + '...</div>';
    
    fetch('/get-table-data?table=' + encodeURIComponent(tableName))
        .then(res => res.json())
        .then(data => {
            if (data.success && data.table) {
                const table = data.table;
                const cols = table.columns || [];
                const rows = table.rows || [];
                const noRows = `<tr><td colspan="${cols.length || 1}" style="text-align:center; color: var(--text-muted); padding: 20px;">
                                    <i class="fas fa-inbox"></i> No rows found
                                </td></tr>`;
                let html = `
                    <div class="table-detail-view">
                        <div class="detail-header">
                            <strong><i class="fas fa-table"></i> ${escapeHtml(tableName)}</strong>
                            <button class="btn btn-secondary" onclick="closeTableDetail()">
                                <i class="fas fa-times"></i> Close
                            </button>
                        </div>
                        <div style="overflow-x:auto; max-height: 300px; overflow-y: auto;">
                            <table>
                                <thead>
                                    <tr>
                                        ${cols.map(col => `<th>${escapeHtml(col)}</th>`).join('')}
                                    </tr>
                                </thead>
                                <tbody>
                                    ${rows.length > 0 ? rows.map(row => `
                                        <tr>
                                            ${row.map(cell => `<td>${cell !== null && cell !== undefined ? escapeHtml(String(cell)) : 'NULL'}</td>`).join('')}
                                        </tr>
                                    `).join('') : noRows}
                                </tbody>
                            </table>
                        </div>
                        <div style="margin-top: 10px; color: var(--text-muted); font-size: 12px;">
                            ${table.row_count} row(s) • ${cols.length} column(s)
                        </div>
                    </div>
                `;
                container.innerHTML = html;
            } else {
                container.innerHTML = '<div class="loading-tables">Error: ' + escapeHtml(data.error || 'Could not load table data') + '</div>';
            }
        })
        .catch(error => {
            console.error('Error loading table details:', error);
            container.innerHTML = '<div class="loading-tables">Error loading table: ' + escapeHtml(error.message) + '</div>';
        });
}

function closeTableDetail() {
    document.getElementById('table-detail-container').innerHTML = '';
}

function updateCurrentDatabaseDisplay(dbName) {
    currentDb = dbName || 'None';
    const displayName = dbName || 'None';
    currentDatabase.textContent = 'Database: ' + displayName;
    chatCurrentDb.textContent = displayName;
}

function updateUIAfterConnect(data) {
    statusIndicator.className = 'status-indicator status-connected';
    statusText.textContent = 'Connected to ' + data.db_type;
    statusText.style.color = 'var(--success-color)';
    
    updateCurrentDatabaseDisplay(data.database);
    
    connectBtn.style.display = 'none';
    disconnectBtn.style.display = 'inline-flex';
    
    queryInput.disabled = false;
    executeBtn.disabled = false;
    clearBtn.disabled = false;
    
    queryInput.focus();
}

function updateUIAfterDisconnect() {
    statusIndicator.className = 'status-indicator status-disconnected';
    statusText.textContent = 'Disconnected';
    statusText.style.color = '';
    
    currentDatabase.textContent = '';
    chatCurrentDb.textContent = 'None';
    
    connectBtn.style.display = 'inline-flex';
    disconnectBtn.style.display = 'none';
    
    queryInput.disabled = true;
    executeBtn.disabled = true;
    clearBtn.disabled = true;
    
    databaseExplorer.style.display = 'none';
    databaseList.innerHTML = '';
}

function showMessage(text, type) {
    connectionMessage.textContent = text;
    connectionMessage.className = 'message ' + type;
    connectionMessage.style.display = 'block';
}

function clearMessage() {
    connectionMessage.textContent = '';
    connectionMessage.className = 'message';
    connectionMessage.style.display = 'none';
}

function addSystemMessage(text) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message system';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = '<i class="fas fa-info-circle"></i> ' + escapeHtml(text);
    
    messageDiv.appendChild(contentDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addUserMessage(query) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message user';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = query;
    
    const timeDiv = document.createElement('div');
    timeDiv.className = 'message-time';
    timeDiv.textContent = new Date().toLocaleTimeString();
    
    messageDiv.appendChild(contentDiv);
    messageDiv.appendChild(timeDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addResultMessage(result) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message result';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    
    if (result.columns && Array.isArray(result.columns) && result.columns.length > 0) {
        let html = '<div style="overflow-x:auto; max-height: 400px; overflow-y: auto;">';
        html += '<table class="result-table">';
        
        html += '<thead><tr>';
        result.columns.forEach(col => {
            html += '<th>' + escapeHtml(col) + '</th>';
        });
        html += '</tr></thead>';
        
        html += '<tbody>';
        if (result.rows && result.rows.length > 0) {
            result.rows.forEach(row => {
                html += '<tr>';
                if (Array.isArray(row)) {
                    row.forEach(cell => {
                        html += '<td>' + (cell !== null && cell !== undefined ? escapeHtml(String(cell)) : 'NULL') + '</td>';
                    });
                }
                html += '</tr>';
            });
        } else {
            const colCount = result.columns.length || 1;
            html += '<tr><td colspan="' + colCount + '" style="text-align:center; color: var(--text-muted); padding: 20px;">';
            html += '<i class="fas fa-inbox"></i> No results found';
            html += '</td></tr>';
        }
        html += '</tbody></table>';
        html += '<div class="result-info"> ' + (result.row_count || 0) + ' row(s) returned</div>';
        html += '</div>';
        
        contentDiv.innerHTML = html;
    } else if (result.message) {
        contentDiv.innerHTML = '<i class="fas fa-check-circle" style="color: var(--success-color);"></i> ' + escapeHtml(result.message);
    } else {
        contentDiv.innerHTML = '<i class="fas fa-info-circle"></i> Query executed successfully but no data returned';
    }
    
    const timeDiv = document.createElement('div');
    timeDiv.className = 'message-time';
    timeDiv.textContent = new Date().toLocaleTimeString();
    
    messageDiv.appendChild(contentDiv);
    messageDiv.appendChild(timeDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addErrorMessage(error) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message error';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = '<i class="fas fa-exclamation-circle"></i> ' + escapeHtml(error);
    
    const timeDiv = document.createElement('div');
    timeDiv.className = 'message-time';
    timeDiv.textContent = new Date().toLocaleTimeString();
    
    messageDiv.appendChild(contentDiv);
    messageDiv.appendChild(timeDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addTableNotFoundMessage(message) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message error';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = '<i class="fas fa-table"></i> ' + escapeHtml(message);
    
    const timeDiv = document.createElement('div');
    timeDiv.className = 'message-time';
    timeDiv.textContent = new Date().toLocaleTimeString();
    
    messageDiv.appendChild(contentDiv);
    messageDiv.appendChild(timeDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addClarificationMessage(message) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message clarification';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.innerHTML = '<i class="fas fa-question-circle"></i> ' + escapeHtml(message);
    
    const timeDiv = document.createElement('div');
    timeDiv.className = 'message-time';
    timeDiv.textContent = new Date().toLocaleTimeString();
    
    messageDiv.appendChild(contentDiv);
    messageDiv.appendChild(timeDiv);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Initialize
function init() {
    loadQueryHistory();
    updateUIAfterDisconnect();

    setTimeout(() => {
        addSystemMessage(' Connect to your database using the form on the left.');
        addSystemMessage(' Try: <b>SHOW DATABASES;</b> to see all databases');
        addSystemMessage(' Use: <b>USE database_name;</b> to switch databases');
        addSystemMessage(' Then: <b>SHOW TABLES;</b> to see tables in the current database');
        addSystemMessage(' Press <b>Enter</b> to execute your query');
        addSystemMessage(' Press <b>Shift+Enter</b> to add a new line');
    }, 500);
}



function scrollToTableViewer() {
    const tableViewer = document.getElementById('table-viewer');
    if (tableViewer) {
        // Wait a moment for the table viewer to render
        setTimeout(() => {
            tableViewer.scrollIntoView({ 
                behavior: 'smooth', 
                block: 'start',
                inline: 'nearest'
            });
            console.log("📜 Scrolled to table viewer");
        }, 800); // 800ms delay to allow tables to load
    }
}

init();