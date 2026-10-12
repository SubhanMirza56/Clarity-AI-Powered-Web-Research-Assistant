(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  const state = { articles: [], current: null, chat: [], activeTab: 'overview', summaryMode: 'brief', importing: false, summarizing: false, chatting: false };
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const website = (url) => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; } };
  const prettyDate = (date) => { if (!date) return 'Date not provided'; const parsed = new Date(date); return isNaN(parsed.getTime()) ? date : new Intl.DateTimeFormat('en', {month:'short', day:'numeric', year:'numeric'}).format(parsed); };
  const toast = (message) => { const el = $('toast'); el.textContent = message; el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3700); };
  const notice = (message, success=false) => { $('notice').textContent = message; $('notice').classList.toggle('success', success); $('notice').classList.remove('hidden'); };
  const clearNotice = () => $('notice').classList.add('hidden');
  const request = async (path, opts={}) => {
    let response;
    try { response = await fetch(path, { ...opts, headers: opts.body ? {'Content-Type': 'application/json', ...(opts.headers || {})} : opts.headers }); }
    catch { throw new Error('Could not connect to the reading workspace. Is the server running?'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Something went wrong. Please try again.');
    return data;
  };
  const paragraphs = (value) => escapeHtml(value || '').split(/\n\s*\n/).filter(Boolean).map(part => `<p>${part.replace(/\n/g, '<br>')}</p>`).join('');
  const markedText = (value) => {
    // Simple safe formatting for answer text; no unsanitized HTML or remote scripts.
    const text = escapeHtml(value || '');
    return text.split(/\n\s*\n/).map(part => `<p>${part.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>')}</p>`).join('');
  };

  async function refreshLibrary() {
    try { state.articles = await request('/api/articles'); renderLibrary(); }
    catch (err) { notice(err.message); }
  }

  function renderLibrary() {
    const term = $('librarySearch').value.trim().toLowerCase();
    const items = state.articles.filter(a => (`${a.title} ${a.url}`).toLowerCase().includes(term));
    $('libraryCount').textContent = state.articles.length;
    $('libraryList').innerHTML = items.length ? items.map(a => `
      <button type="button" class="library-item ${state.current?.id === a.id ? 'active' : ''}" data-id="${escapeHtml(a.id)}">
        <span class="lib-icon">${icon('file')}</span><span class="lib-body"><strong>${escapeHtml(a.title)}</strong><span>${escapeHtml(website(a.url))} · ${a.reading_minutes} min read</span></span>
      </button>`).join('') : `<p class="library-empty">${term ? 'No articles match that search.' : 'Your saved articles will appear here.'}</p>`;
    $('libraryList').querySelectorAll('[data-id]').forEach(button => button.addEventListener('click', () => selectArticle(button.dataset.id)));
  }

  function setImportBusy(busy) {
    state.importing = busy;
    $('importBtn').disabled = busy;
    $('importBtn').querySelector('span').textContent = busy ? 'Opening article...' : 'Explore article';
    $('importProgress').textContent = busy ? 'Retrieving and organizing article...' : '';
  }

  async function importUrl(value, refresh=false) {
    if (state.importing) return;
    clearNotice();
    setImportBusy(true);
    try {
      const article = await request('/api/articles/import', {method:'POST', body: JSON.stringify({url:value, refresh})});
      await refreshLibrary();
      await showArticle(article);
      toast('Article added to your reading space.');
      if (!article.insights) createInsights(true);
    } catch (err) { notice(err.message); }
    finally { setImportBusy(false); }
  }

  async function selectArticle(id) {
    clearNotice();
    try {
      const article = await request(`/api/articles/${encodeURIComponent(id)}`);
      await showArticle(article);
      if (window.innerWidth < 740) $('sidebar').classList.remove('open');
      $('articleSpace').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (err) { notice(err.message); }
  }

  async function showArticle(article) {
    state.current = article;
    state.chat = [];
    state.summaryMode = 'brief';
    $('urlInput').value = article.url;
    $('articleTitle').textContent = article.title;
    $('articleDomain').innerHTML = `${icon('globe')}${escapeHtml(website(article.url))}`;
    $('articleDate').innerHTML = `${icon('calendar')}${escapeHtml(prettyDate(article.published_time))}`;
    $('articleRead').innerHTML = `${icon('clock')}${article.reading_minutes} min read · ${article.word_count.toLocaleString()} words`;
    $('articleLink').href = article.url;
    $('originalText').textContent = article.content;
    $('emptyState').classList.add('hidden');
    $('articleSpace').classList.remove('hidden');
    renderInsights(article.insights);
    renderLibrary();
    switchTab('overview');
    try {
      state.chat = await request(`/api/articles/${encodeURIComponent(article.id)}/chat`);
      renderChat();
    } catch (err) { toast(`Couldn't load previous conversation: ${err.message}`); }
  }

  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll('[data-tab]').forEach(button => {
      const active = button.dataset.tab === tab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    for (const name of ['overview','article','chat']) $('pane-' + name).classList.toggle('hidden', tab !== name);
    if (tab === 'chat') setTimeout(() => $('chatMessages').scrollTop = $('chatMessages').scrollHeight, 0);
  }

  function renderInsights(notes) {
    const ready = notes && notes.summary;
    $('briefShort').classList.toggle('active', state.summaryMode === 'brief');
    $('briefLong').classList.toggle('active', state.summaryMode !== 'brief');
    $('summaryText').innerHTML = ready ? paragraphs(state.summaryMode === 'brief' ? notes.summary : notes.detailed_summary) : '<div class="placeholder-brief"><span class="placeholder-line w90"></span><span class="placeholder-line w100"></span><span class="placeholder-line w70"></span><p>Your article overview will appear here.</p></div>';
    $('keyPoints').innerHTML = notes?.key_points?.length ? notes.key_points.map((item, i) => `<div class="point-item"><span class="point-number">${String(i + 1).padStart(2, '0')}</span><span>${escapeHtml(item)}</span></div>`).join('') : '<p class="muted-placeholder">Important ideas from this page will appear here.</p>';
    $('suggestedQuestions').innerHTML = notes?.questions?.length ? notes.questions.map(item => `<button class="question-link" data-question="${escapeHtml(item)}"><span>${escapeHtml(item)}</span>${icon('arrow')}</button>`).join('') : '<p class="muted-placeholder">Thoughtful prompts will appear after your brief is ready.</p>';
    $('keyTerms').innerHTML = notes?.key_terms?.length ? notes.key_terms.map(item => `<span class="topic-pill">${escapeHtml(item)}</span>`).join('') : '<span class="topic-pill">Waiting for your brief</span>';
    $('generateBtn').querySelector('span').textContent = ready ? 'Brief is ready' : (state.summarizing ? 'Preparing brief...' : 'Prepare my brief');
    $('generateBtn').disabled = !!ready || state.summarizing;
    document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
      switchTab('chat');
      $('questionInput').value = button.dataset.question;
      $('questionInput').focus();
    }));
  }

  async function createInsights(silent=false) {
    const current = state.current;
    if (!current || current.insights || state.summarizing) return;
    state.summarizing = true;
    renderInsights(null);
    try {
      const notes = await request(`/api/articles/${encodeURIComponent(current.id)}/insights`, {method:'POST'});
      // Ignore a late response if the user switched to another article.
      if (state.current?.id === current.id) {
        state.current.insights = notes;
        renderInsights(notes);
        toast('Your reading brief is ready.');
      }
    } catch (err) { if (!silent) notice(err.message); else toast(`Brief not ready: ${err.message}`); }
    finally { state.summarizing = false; if (state.current) renderInsights(state.current.insights); }
  }

  function renderChat() {
    const el = $('chatMessages');
    if (!state.chat.length) {
      el.innerHTML = `<div class="chat-welcome"><div class="chat-welcome-icon">${icon('spark')}</div><h3>What would you like to understand?</h3><p>Ask about details, claims, context, or conclusions. Responses are grounded in the article, with passages you can check.</p><div class="welcome-suggestions"><button data-prompt="What is the main argument of this article?">What is the main argument?</button><button data-prompt="What evidence does the article provide?">What evidence is provided?</button></div></div>`;
    } else {
      el.innerHTML = state.chat.map(msg => `<div class="message ${msg.role === 'user' ? 'user' : 'assistant'}"><span class="msg-avatar">${icon(msg.role === 'user' ? 'message' : 'spark')}</span><div><div class="msg-bubble">${markedText(msg.content)}</div>${msg.sources?.length ? `<div class="sources-label">Passages used</div>${msg.sources.map(source => `<details class="source-button"><summary>Source [${source.number}] · Section ${source.section}</summary><p>${escapeHtml(source.excerpt)}</p></details>`).join('')}` : ''}</div></div>`).join('');
    }
    el.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
      $('questionInput').value = button.dataset.prompt;
      $('questionInput').focus();
    }));
    el.scrollTop = el.scrollHeight;
  }

  async function sendQuestion(question) {
    const current = state.current;
    if (!current || state.chatting || !question.trim()) return;
    state.chatting = true;
    $('sendBtn').disabled = true;
    state.chat.push({role:'user', content:question, sources:[]});
    $('questionInput').value = '';
    renderChat();
    try {
      const result = await request(`/api/articles/${encodeURIComponent(current.id)}/chat`, {method:'POST', body:JSON.stringify({question})});
      if (state.current?.id === current.id) {
        state.chat.push({role:'assistant', content:result.answer, sources:result.sources});
        renderChat();
      }
    } catch (err) {
      if (state.current?.id === current.id) {
        state.chat.pop();
        renderChat();
        $('questionInput').value = question;
        notice(err.message);
      }
    } finally { state.chatting = false; $('sendBtn').disabled = false; }
  }

  function openEmpty() {
    state.current = null;
    $('urlInput').value = '';
    $('articleSpace').classList.add('hidden');
    $('emptyState').classList.remove('hidden');
    $('urlInput').focus();
    renderLibrary();
    if (window.innerWidth < 740) $('sidebar').classList.remove('open');
    window.scrollTo({top:0, behavior:'smooth'});
  }

  $('importForm').addEventListener('submit', e => {e.preventDefault(); importUrl($('urlInput').value);});
  $('librarySearch').addEventListener('input', renderLibrary);
  $('newBtn').addEventListener('click', openEmpty);
  $('homeBtn').addEventListener('click', () => {window.scrollTo({top:0, behavior:'smooth'}); $('sidebar').classList.remove('open');});
  $('menuBtn').addEventListener('click', () => $('sidebar').classList.toggle('open'));
  document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => switchTab(button.dataset.tab)));
  $('briefShort').addEventListener('click', () => {state.summaryMode='brief';renderInsights(state.current?.insights);});
  $('briefLong').addEventListener('click', () => {state.summaryMode='detailed';renderInsights(state.current?.insights);});
  $('generateBtn').addEventListener('click', () => createInsights());
  $('chatForm').addEventListener('submit', e => {e.preventDefault(); sendQuestion($('questionInput').value);});
  $('questionInput').addEventListener('keydown', e => {if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('chatForm').requestSubmit(); }});
  $('refreshBtn').addEventListener('click', () => {if (state.current && confirm('Fetch the latest version? This will clear the current chat and reading brief.')) importUrl(state.current.url, true);});
  $('exportBtn').addEventListener('click', () => {if (state.current) window.location.href = `/api/articles/${state.current.id}/export`;});
  $('deleteBtn').addEventListener('click', async () => {
    if (!state.current || !confirm('Remove this article and its conversation from your library?')) return;
    try {await request(`/api/articles/${state.current.id}`, {method:'DELETE'}); openEmpty(); await refreshLibrary(); toast('Article removed from your library.');}
    catch (err) {notice(err.message);}
  });
  $('copyArticle').addEventListener('click', async () => {
    if (!state.current) return;
    try {await navigator.clipboard.writeText(state.current.content); toast('Article text copied.');}
    catch {toast('Clipboard unavailable. Select the text manually.');}
  });
  $('clearChat').addEventListener('click', async () => {
    if (!state.current || !state.chat.length) return;
    if (!confirm('Clear this article conversation?')) return;
    try { await request(`/api/articles/${state.current.id}/chat`, {method:'DELETE'}); state.chat=[]; renderChat(); toast('New conversation ready.'); }
    catch (err) { notice(err.message); }
  });
  (async () => {
    try {const info = await request('/api/health'); $('apiStatus').innerHTML = `<i></i>${info.configured ? ' Workspace ready' : ' Add API keys to begin'}`; $('apiStatus').classList.toggle('unconfigured', !info.configured); }
    catch { $('apiStatus').innerHTML = '<i></i> Server unavailable'; $('apiStatus').classList.add('unconfigured'); }
    await refreshLibrary();
  })();
})();
