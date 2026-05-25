// MedAI — Complete Frontend Logic
// Fixes: null checks, correct DOM IDs, robust SSE, history, PDF

// ========== Configure marked.js ==========
if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
}

// ========== State ==========
let isProcessing = false;
let currentSessionId = null;
let allSessions = [];
let sidebarOpen = window.innerWidth > 768;

// ========== DOM — with null-safe getters ==========
const get = (id) => document.getElementById(id);

const chatContainer  = get('chatContainer');
const userInput      = get('userInput');
const sendBtn        = get('sendBtn');
const clearBtn       = get('clearBtn');
const statusDot      = get('statusDot');
const statusText     = get('statusText');
const charCount      = get('charCount');
const sidebar        = get('sidebar');
const sidebarToggle  = get('sidebarToggle');
const sidebarOverlay = get('sidebarOverlay');
const newChatBtn     = get('newChatBtn');
const historyList    = get('historyList');
const historySearch  = get('historySearch');
const welcomeSection = get('welcomeSection');
const toastContainer = get('toastContainer');

// ========== Init ==========
document.addEventListener('DOMContentLoaded', () => {
    if (userInput) { adjustTextareaHeight(); userInput.focus(); }
    checkHealth();
    fetchSessions();
    setupEventListeners();
    // Restore sidebar state on desktop
    if (window.innerWidth <= 768 && sidebar) {
        sidebar.classList.remove('open');
        sidebarOpen = false;
    }
});

// ========== Health Check ==========
async function checkHealth() {
    try {
        const res = await fetch('/api/health');
        if (!res.ok) throw new Error('non-200');
        const data = await res.json();
        if (data.status !== 'ok' || data.llm !== 'ready') {
            setStatus('AI service issue — check API key', false);
        }
    } catch {
        setStatus('Cannot connect to server', false);
    }
}

function setStatus(text, ready = true) {
    if (statusText) statusText.textContent = text;
    if (statusDot) {
        statusDot.className = 'status-dot' + (ready ? '' : ' thinking');
    }
}

// ========== Event Listeners ==========
function setupEventListeners() {
    if (sendBtn) sendBtn.addEventListener('click', handleSend);

    if (userInput) {
        userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
        });
        userInput.addEventListener('input', () => {
            adjustTextareaHeight();
            updateCharCount();
            if (sendBtn) sendBtn.disabled = !userInput.value.trim() || isProcessing;
        });
    }

    if (sidebarToggle) sidebarToggle.addEventListener('click', toggleSidebar);
    if (sidebarOverlay) sidebarOverlay.addEventListener('click', closeSidebarMobile);

    if (newChatBtn) newChatBtn.addEventListener('click', startNewChat);

    if (historySearch) {
        historySearch.addEventListener('input', (e) => renderSessions(e.target.value || ''));
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            if (!confirm('Clear this conversation from view?')) return;
            currentSessionId = null;
            if (chatContainer) {
                chatContainer.innerHTML = '';
                if (welcomeSection) {
                    welcomeSection.style.opacity = '1';
                    chatContainer.appendChild(welcomeSection);
                }
            }
            renderSessions();
        });
    }

    // Theme toggle
    const themeBtn = get('themeToggleBtn');
    if (themeBtn) {
        if (localStorage.getItem('medai_theme') === 'light') {
            document.body.classList.add('light-mode');
            themeBtn.querySelector('i').className = 'fas fa-sun';
        }
        themeBtn.addEventListener('click', () => {
            document.body.classList.toggle('light-mode');
            const isLight = document.body.classList.contains('light-mode');
            localStorage.setItem('medai_theme', isLight ? 'light' : 'dark');
            themeBtn.querySelector('i').className = isLight ? 'fas fa-sun' : 'fas fa-moon';
            showToast(`Switched to ${isLight ? 'Light' : 'Dark'} mode`, 'info');
        });
    }

    // PDF Download
    const pdfBtn = get('pdfBtn');
    if (pdfBtn) {
        pdfBtn.addEventListener('click', () => {
            if (typeof html2pdf === 'undefined') { showToast('PDF library not loaded', 'error'); return; }
            if (!chatContainer || !chatContainer.querySelector('.message')) {
                showToast('No messages to export', 'error'); return;
            }
            showToast('Generating PDF...', 'info');
            const opt = {
                margin: 10,
                filename: 'MedAI-Chat.pdf',
                image: { type: 'jpeg', quality: 0.95 },
                html2canvas: { scale: 2, backgroundColor: '#0a0e1a' },
                jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' }
            };
            html2pdf().set(opt).from(chatContainer).save()
                .then(() => showToast('PDF downloaded!', 'success'))
                .catch(() => showToast('PDF export failed', 'error'));
        });
    }

    // Quick chips (.chip) and welcome pills (.pill)
    document.querySelectorAll('.chip, .pill').forEach(el => {
        el.addEventListener('click', () => {
            const query = el.dataset.query;
            if (!query || !userInput) return;
            userInput.value = query;
            adjustTextareaHeight();
            updateCharCount();
            if (sendBtn) sendBtn.disabled = false;
            userInput.focus();
            handleSend();
            if (window.innerWidth <= 768) closeSidebarMobile();
        });
    });

    // Window resize
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768 && sidebar) {
            sidebar.classList.remove('open');
            if (sidebarOverlay) sidebarOverlay.classList.remove('active');
        }
    });
}

// ========== Sidebar Toggle ==========
function toggleSidebar() {
    if (!sidebar) return;
    if (window.innerWidth <= 768) {
        sidebarOpen = !sidebarOpen;
        sidebar.classList.toggle('open', sidebarOpen);
        if (sidebarOverlay) sidebarOverlay.classList.toggle('active', sidebarOpen);
    } else {
        sidebarOpen = !sidebarOpen;
        sidebar.classList.toggle('collapsed', !sidebarOpen);
    }
}

function closeSidebarMobile() {
    sidebarOpen = false;
    if (sidebar) sidebar.classList.remove('open');
    if (sidebarOverlay) sidebarOverlay.classList.remove('active');
}

// ========== Chat History ==========
async function fetchSessions() {
    try {
        const res = await fetch('/api/sessions');
        if (!res.ok) return;
        const data = await res.json();
        if (data.success && Array.isArray(data.sessions)) {
            allSessions = data.sessions;
            renderSessions();
        }
    } catch (e) {
        console.error('fetchSessions error:', e);
    }
}

function renderSessions(filterText = '') {
    if (!historyList) return;
    historyList.innerHTML = '';

    const filtered = allSessions.filter(s =>
        (s.title || '').toLowerCase().includes(filterText.toLowerCase())
    );

    if (filtered.length === 0) {
        historyList.innerHTML = `
            <div class="empty-history" id="emptyHistory">
                <i class="fas fa-comment-slash" style="font-size:1.5rem;opacity:0.3;margin-bottom:8px;"></i>
                <p>${filterText ? 'No matching chats' : 'No conversations yet'}</p>
            </div>`;
        return;
    }

    filtered.forEach(session => {
        const item = document.createElement('div');
        item.className = `history-item ${session.id === currentSessionId ? 'active' : ''}`;
        item.setAttribute('role', 'listitem');

        const titleSpan = document.createElement('span');
        titleSpan.className = 'history-title';
        titleSpan.textContent = session.title || 'Untitled Chat';
        titleSpan.title = session.title || '';

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'history-actions';

        // Rename button
        const renameBtn = document.createElement('button');
        renameBtn.className = 'history-action-btn rename';
        renameBtn.innerHTML = '<i class="fas fa-pencil-alt"></i>';
        renameBtn.title = 'Rename';
        renameBtn.onclick = (e) => { e.stopPropagation(); renameSession(session.id, session.title); };

        // Delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'history-action-btn delete';
        deleteBtn.innerHTML = '<i class="fas fa-trash-alt"></i>';
        deleteBtn.title = 'Delete';
        deleteBtn.onclick = (e) => { e.stopPropagation(); deleteSession(session.id); };

        actionsDiv.appendChild(renameBtn);
        actionsDiv.appendChild(deleteBtn);
        item.appendChild(titleSpan);
        item.appendChild(actionsDiv);
        item.addEventListener('click', () => loadSession(session.id));
        historyList.appendChild(item);
    });
}

function startNewChat() {
    currentSessionId = null;
    if (chatContainer) {
        chatContainer.innerHTML = '';
        if (welcomeSection) {
            welcomeSection.style.opacity = '1';
            chatContainer.appendChild(welcomeSection);
        }
    }
    renderSessions(historySearch ? (historySearch.value || '') : '');
    if (userInput) userInput.focus();
    if (window.innerWidth <= 768) closeSidebarMobile();
}

async function loadSession(sessionId) {
    if (!sessionId) return;
    currentSessionId = sessionId;
    renderSessions(historySearch ? (historySearch.value || '') : '');
    if (window.innerWidth <= 768) closeSidebarMobile();

    if (chatContainer) chatContainer.innerHTML = '';
    setStatus('Loading chat...', true);
    isProcessing = true;

    try {
        const res = await fetch(`/api/sessions/${sessionId}/messages`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.success && Array.isArray(data.messages)) {
            data.messages.forEach(msg => {
                const role = (msg.role || '').toLowerCase();
                const sender = role === 'user' ? 'user' : 'bot';
                const content = msg.content || '';
                const { msgEl, contentEl } = addMessage(content, sender, msg.id);
                if (sender === 'bot') {
                    renderMarkdown(contentEl, content);
                    addMessageActions(msgEl, content);
                }
            });
            scrollToBottom();
        }
    } catch (e) {
        console.error('loadSession error:', e);
        showToast('Failed to load chat', 'error');
    } finally {
        isProcessing = false;
        setStatus('Online & Ready to Help', true);
        if (userInput) userInput.focus();
    }
}

async function deleteSession(sessionId) {
    if (!confirm('Delete this conversation permanently?')) return;
    try {
        const res = await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            showToast('Conversation deleted', 'success');
            if (currentSessionId === sessionId) startNewChat();
            allSessions = allSessions.filter(s => s.id !== sessionId);
            renderSessions(historySearch ? (historySearch.value || '') : '');
        }
    } catch (e) {
        showToast('Failed to delete', 'error');
    }
}

async function renameSession(sessionId, currentTitle) {
    const newTitle = prompt('Rename conversation:', currentTitle || '');
    if (!newTitle || !newTitle.trim()) return;
    try {
        const res = await fetch(`/api/sessions/${sessionId}/rename`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle.trim() })
        });
        const data = await res.json();
        if (data.success) {
            const s = allSessions.find(x => x.id === sessionId);
            if (s) s.title = newTitle.trim();
            renderSessions(historySearch ? (historySearch.value || '') : '');
            showToast('Renamed successfully', 'success');
        }
    } catch {
        showToast('Rename failed', 'error');
    }
}

// ========== Handle Send ==========
async function handleSend() {
    if (!userInput) return;
    const message = (userInput.value || '').trim();
    if (!message || isProcessing) return;

    // Hide welcome section
    if (welcomeSection && welcomeSection.parentNode === chatContainer) {
        welcomeSection.style.transition = 'opacity 0.3s';
        welcomeSection.style.opacity = '0';
        setTimeout(() => { if (welcomeSection.parentNode) welcomeSection.parentNode.removeChild(welcomeSection); }, 300);
    }

    userInput.value = '';
    adjustTextareaHeight();
    if (charCount) charCount.textContent = '0 / 2000';

    addMessage(message, 'user');
    setProcessing(true);

    await streamResponse(message);

    setProcessing(false);
    if (userInput) userInput.focus();
}

// ========== SSE Streaming ==========
async function streamResponse(message, editMessageId = null) {
    // 1. Add "Thinking" indicator first
    const { msgEl, contentEl } = addMessage('', 'bot');
    contentEl.innerHTML = `
        <div class="typing-indicator" id="typingIndicator">
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
        </div>
    `;

    let fullText = '';
    let firstTokenReceived = false;
    const cursorEl = document.createElement('span');
    cursorEl.className = 'streaming-cursor';

    const payload = { message, session_id: currentSessionId };
    if (editMessageId) payload.edit_message_id = editMessageId;

    try {
        const response = await fetch('/api/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.error || `HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';  // keep incomplete line

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const raw = line.slice(6).trim();
                if (!raw) continue;
                try {
                    const payload = JSON.parse(raw);

                    if (payload.error) {
                        if (cursorEl.parentNode) cursorEl.remove();
                        contentEl.innerHTML = `<span style="color:#ef4444;">⚠ ${payload.error}</span>`;
                        return;
                    }

                    if (payload.session_id && !payload.done) {
                        // New session created
                        currentSessionId = payload.session_id;
                        // Add to local sessions list
                        if (!allSessions.find(s => s.id === payload.session_id)) {
                            allSessions.unshift({ id: payload.session_id, title: payload.title || message.slice(0, 35), updated_at: new Date().toISOString() });
                            renderSessions(historySearch ? (historySearch.value || '') : '');
                        }
                    } else if (payload.token) {
                        if (!firstTokenReceived) {
                            firstTokenReceived = true;
                            contentEl.innerHTML = '';
                            contentEl.appendChild(cursorEl);
                        }
                        fullText += payload.token;
                        // Compile and render Markdown dynamically as tokens arrive
                        if (typeof marked !== 'undefined') {
                            contentEl.innerHTML = marked.parse(fullText);
                        } else {
                            contentEl.innerHTML = escapeStreamingText(fullText);
                        }
                        contentEl.appendChild(cursorEl);
                        scrollToBottom();
                    } else if (payload.done) {
                        if (cursorEl.parentNode) cursorEl.remove();
                        renderMarkdown(contentEl, fullText);
                        addMessageActions(msgEl, fullText);
                        scrollToBottom();
                        // Refresh session list
                        fetchSessions();
                    }
                } catch { /* ignore malformed JSON lines */ }
            }
        }

        // Fallback: stream ended without done signal
        if (cursorEl.parentNode) {
            cursorEl.remove();
            if (fullText) {
                renderMarkdown(contentEl, fullText);
                addMessageActions(msgEl, fullText);
            }
            scrollToBottom();
        }

    } catch (err) {
        console.error('streamResponse error:', err);
        if (cursorEl.parentNode) cursorEl.remove();
        if (fullText) {
            renderMarkdown(contentEl, fullText);
            addMessageActions(msgEl, fullText);
        } else {
            contentEl.innerHTML = '<span style="color:#ef4444;">⚠ Connection error. Please try again.</span>';
        }
        scrollToBottom();
        showToast(err.message || 'Streaming failed', 'error');
    }
}

// ========== Add Message Bubble ==========
function addMessage(text, sender, msgId = null) {
    const isBot = sender === 'bot';
    const msgEl = document.createElement('div');
    msgEl.className = `message ${sender}`;

    const avatarEl = document.createElement('div');
    avatarEl.className = 'msg-avatar';
    avatarEl.innerHTML = isBot
        ? '<i class="fas fa-heartbeat" style="font-size:12px;"></i>'
        : '<i class="fas fa-user"></i>';

    const bodyEl = document.createElement('div');
    bodyEl.className = 'msg-body';

    const contentEl = document.createElement('div');
    contentEl.className = 'message-content';
    if (text) contentEl.textContent = text;

    bodyEl.appendChild(contentEl);

    // Edit button for user messages with DB ID
    if (sender === 'user' && msgId) {
        const editBtn = document.createElement('button');
        editBtn.className = 'edit-btn';
        editBtn.innerHTML = '<i class="fas fa-pencil-alt" style="font-size:10px;"></i>';
        editBtn.title = 'Edit & Resend';
        editBtn.onclick = () => renderEditInterface(contentEl, msgId, text);
        bodyEl.appendChild(editBtn);
    }

    msgEl.appendChild(isBot ? avatarEl : document.createElement('div'));
    if (!isBot) msgEl.insertBefore(bodyEl, msgEl.firstChild);
    else msgEl.appendChild(bodyEl);

    // Fix: for user messages avatar on right, bot on left
    if (isBot) {
        msgEl.insertBefore(avatarEl, bodyEl);
    }

    if (chatContainer) { chatContainer.appendChild(msgEl); scrollToBottom(); }
    return { msgEl, contentEl, bodyEl };
}

function renderEditInterface(contentEl, msgId, originalText) {
    contentEl.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'edit-mode-wrap';
    const textarea = document.createElement('textarea');
    textarea.className = 'edit-textarea';
    textarea.value = originalText || '';
    const actions = document.createElement('div');
    actions.className = 'edit-actions';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'edit-save';
    saveBtn.textContent = 'Save & Resend';
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'edit-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = () => { contentEl.textContent = originalText || ''; };
    saveBtn.onclick = async () => {
        const newText = (textarea.value || '').trim();
        if (!newText) { showToast('Query cannot be empty', 'error'); return; }
        if (chatContainer) chatContainer.innerHTML = '';
        setProcessing(true);
        await streamResponse(newText, msgId);
        setProcessing(false);
        if (currentSessionId) loadSession(currentSessionId);
    };
    actions.appendChild(cancelBtn);
    actions.appendChild(saveBtn);
    wrap.appendChild(textarea);
    wrap.appendChild(actions);
    contentEl.appendChild(wrap);
}

// ========== Markdown & Formatting ==========
function renderMarkdown(el, text) {
    if (!el) return;
    if (typeof marked !== 'undefined' && text) {
        el.innerHTML = marked.parse(text);
    } else {
        el.textContent = text || '';
    }
}

function escapeStreamingText(text) {
    if (typeof text !== 'string') return '';
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '<br>');
}

// ========== Message Actions (timestamp + copy) ==========
function addMessageActions(msgEl, text) {
    if (!msgEl) return;
    const bodyEl = msgEl.querySelector('.msg-body');
    if (!bodyEl) return;

    const actionsEl = document.createElement('div');
    actionsEl.className = 'msg-actions';

    const timeEl = document.createElement('span');
    timeEl.className = 'msg-time';
    timeEl.textContent = getTime();
    actionsEl.appendChild(timeEl);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.title = 'Copy response';
    copyBtn.innerHTML = '<i class="fas fa-copy" style="font-size:11px;"></i>';
    copyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(text || '').then(() => {
            copyBtn.innerHTML = '<i class="fas fa-check" style="font-size:11px;"></i>';
            copyBtn.classList.add('copied');
            showToast('Copied to clipboard', 'success');
            setTimeout(() => {
                copyBtn.innerHTML = '<i class="fas fa-copy" style="font-size:11px;"></i>';
                copyBtn.classList.remove('copied');
            }, 2000);
        }).catch(() => showToast('Copy failed', 'error'));
    });
    actionsEl.appendChild(copyBtn);
    bodyEl.appendChild(actionsEl);
}

// ========== Processing State ==========
function setProcessing(active) {
    isProcessing = active;
    if (sendBtn) sendBtn.disabled = active;
    if (active) {
        setStatus('AI is thinking...', false);
        if (statusDot) statusDot.classList.add('thinking');
    } else {
        setStatus('Online & Ready to Help', true);
        if (statusDot) statusDot.classList.remove('thinking');
    }
}

// ========== Helpers ==========
function adjustTextareaHeight() {
    if (!userInput) return;
    userInput.style.height = 'auto';
    userInput.style.height = Math.min(userInput.scrollHeight, 140) + 'px';
}

function updateCharCount() {
    if (!userInput || !charCount) return;
    const len = (userInput.value || '').length;
    charCount.textContent = `${len} / 2000`;
    charCount.className = 'char-count' + (len > 1800 ? ' danger' : len > 1500 ? ' warn' : '');
    if (sendBtn) sendBtn.disabled = len === 0 || isProcessing;
}

function scrollToBottom() {
    if (chatContainer) {
        chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    }
}

function getTime() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function showToast(message, type = 'info') {
    if (!toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const iconMap = { success: 'check-circle', error: 'exclamation-triangle', info: 'info-circle' };
    toast.innerHTML = `<i class="fas fa-${iconMap[type] || 'info-circle'}"></i> <span>${message}</span>`;
    toastContainer.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 350);
    }, 3500);
}
