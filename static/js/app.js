// Must match SECRET_MASK in config.py: the placeholder the API returns instead
// of a stored secret. Sending it back unchanged keeps the stored value.
const SECRET_MASK = '••••••••••••';

// OpenAI models offered in the Settings dropdowns (checked 1 Oct 2026). Anything
// else is entered through "Model lain…".
const OPENAI_MODELS = {
    text: ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-astra', 'gpt-5.6-terra'],
    image: ['gpt-image-2.5-flare', 'gpt-image-2.5-sunburst', 'gpt-image-2'],
};

// A 401 from the API means the login session ended (logged out elsewhere, password
// changed, expired): send the browser back to the login page.
if (typeof window !== 'undefined') {   // absent when the UI tests load this file in node
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const res = await nativeFetch(...args);
        if (res.status === 401 && !String(args[0]).startsWith('/api/auth/')) window.location.replace('/login');
        return res;
    };
}

function autoPosterApp() {
    return {
        activeTab: 'generator',
        draftSnapshot: '', isSavingDraft: false, isRegenerating: false, isRewritingCaption: false, isRefreshingMetrics: false,
        authEmail: '', loggingOut: false, isChangingPassword: false,
        passwordForm: { current: '', next: '', confirm: '' },
        loadedPageId: null, isLoadingPage: false, requestIds: {}, dataErrors: {},
        historySearch: '', historyStatus: '', topicsExpanded: false,
        // Last connection check per provider and the key/model snapshot it applies to.
        providerChecks: { gemini: { state: 'Belum diverifikasi', for: null }, openai: { state: 'Belum diverifikasi', for: null } },
        savingPageId: null,
        get draftDirty() {
            return this.currentPost && this.currentPost.status !== 'published' &&
                this.draftSnapshot !== JSON.stringify([this.currentPost.visual_title, this.currentPost.caption]);
        },
        get hasUnsavedChanges() {
            return this.draftDirty || this.settingsDirty || this.pages.some(p => p.dirty) || this.replyFormDirty;
        },
        get readyToGenerate() {
            return !this.aiMissingKey && !this.settingsDirty && !!this.activePage && !this.isLoadingPage;
        },
        // ---------- AI providers (text and image are chosen separately) ----------
        providerLabel(p) { return p === 'openai' ? 'OpenAI' : 'Gemini'; },
        get usedProviders() {
            return [...new Set([this.settings.text_provider || 'gemini', this.settings.image_provider || 'gemini'])];
        },
        get aiLabel() { return this.usedProviders.map(p => this.providerLabel(p)).join(' + '); },
        get aiMissingKey() {
            const missing = this.usedProviders.find(p => !this.settings[`${p}_api_key`]);
            return missing ? this.providerLabel(missing) : '';
        },
        openaiCustomModel: { text: false, image: false },
        openaiModelChoice(role) {
            const value = this.settings[`openai_${role}_model`];
            return !this.openaiCustomModel[role] && OPENAI_MODELS[role].includes(value) ? value : '__custom';
        },
        chooseOpenaiModel(role, value) {
            this.openaiCustomModel[role] = value === '__custom';
            if (value !== '__custom') this.settings[`openai_${role}_model`] = value;
            this.settingsDirty = true;
        },
        providerSnapshot(p) {
            return JSON.stringify(p === 'openai'
                ? [this.settings.openai_api_key, this.settings.openai_text_model, this.settings.openai_image_model]
                : [this.settings.gemini_api_key, this.settings.gemini_text_model]);
        },
        // A check only counts for the exact key and model(s) it tested.
        providerState(p) {
            const check = this.providerChecks[p];
            return check.for === this.providerSnapshot(p) ? check.state : 'Belum diverifikasi';
        },
        recordCheck(p, state, snapshot) { this.providerChecks[p] = { state, for: snapshot }; },
        get aiState() {
            if (this.aiMissingKey) return 'Belum diisi';
            const states = this.usedProviders.map(p => this.providerState(p));
            if (states.includes('Gagal')) return 'Gagal';
            return states.every(st => st === 'Terhubung') ? 'Terhubung' : 'Belum diverifikasi';
        },
        get activeTextModel() {
            return this.settings.text_provider === 'openai' ? this.settings.openai_text_model : this.settings.gemini_text_model;
        },
        get activeImageModel() {
            return this.settings.image_provider === 'openai' ? this.settings.openai_image_model : this.settings.gemini_image_model;
        },
        acceptPost(post) {
            this.requestIds.detail = (this.requestIds.detail || 0) + 1;
            this.currentPost = post;
            this.draftSnapshot = JSON.stringify([post.visual_title, post.caption]);
            this.patchFeedItem(post);
        },
        leaveDraft(action) {
            if (!this.draftDirty) { action(); return; }
            this.askConfirm({title: 'Perubahan draft belum disimpan',
                message: 'Simpan draft terlebih dahulu, atau lanjutkan dan abaikan perubahan ini.',
                confirmLabel: 'Abaikan perubahan', tone: 'danger', action});
        },
        async saveDraft() {
            if (!this.currentPost || this.isSavingDraft) return;
            const post = this.currentPost;
            const payload = {caption: post.caption, visual_title: post.visual_title};
            this.isSavingDraft = true;
            try {
                const res = await fetch(`/api/posts/${post.id}`, {method: 'PATCH',
                    headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)});
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.detail || data.message || 'Gagal menyimpan draft.');
                if (this.currentPost?.id === post.id) {
                    if (this.currentPost.visual_title === payload.visual_title) this.currentPost.visual_title = payload.visual_title.trim();
                    this.draftSnapshot = JSON.stringify([payload.visual_title.trim(), payload.caption]);
                }
                this.showToast('Draft tersimpan.');
                await this.fetchPosts();
            } catch (err) { this.showToast(err.message, 'error'); }
            finally { this.isSavingDraft = false; }
        },
        async scopedData(key, url) {
            const page = this.activePageId;
            const id = (this.requestIds[key] || 0) + 1;
            this.requestIds[key] = id;
            try {
                const res = await fetch(url);
                if (!res.ok) throw new Error('Gagal memuat data.');
                const data = await res.json();
                if (page !== this.activePageId || id !== this.requestIds[key]) return null;
                delete this.dataErrors[key];
                return data;
            } catch (err) {
                if (page === this.activePageId && id === this.requestIds[key]) this.dataErrors[key] = 'Pembaruan gagal. Data yang tampil mungkin belum terbaru.';
                return null;
            }
        },
        async retryData() {
            await Promise.all([this.fetchPosts(), this.fetchAnalytics(), this.fetchTopics(), this.fetchWindowStats(), this.fetchScheduleStatus(), this.fetchPages()]);
        },
        // ---------- comment auto-reply ----------
        replies: { items: [], total: 0, counts: {}, has_more: false },
        replyStatus: '',
        isLoadingReplies: false,
        isScanningReplies: false,
        isSavingReplySettings: false,
        replyForm: { auto_reply_enabled: false, auto_reply_mode: 'auto', reply_max_per_hour: 20 },
        replyFilters: [
            { value: '', label: 'Semua' },
            { value: 'pending', label: 'Menunggu persetujuan' },
            { value: 'replied', label: 'Terkirim' },
            { value: 'skipped', label: 'Dilewati' },
            { value: 'failed', label: 'Gagal' },
            { value: 'dismissed', label: 'Diabaikan' }
        ],
        replyFormFrom(p) {
            return { auto_reply_enabled: !!p.auto_reply_enabled, auto_reply_mode: p.auto_reply_mode || 'auto',
                     reply_max_per_hour: p.reply_max_per_hour || 20 };
        },
        syncReplyForm() {
            if (this.activePage) this.replyForm = this.replyFormFrom(this.activePage);
        },
        get replyFormDirty() {
            if (!this.activePage) return false;
            return JSON.stringify(this.replyFormFrom(this.activePage)) !==
                   JSON.stringify({ ...this.replyForm, reply_max_per_hour: parseInt(this.replyForm.reply_max_per_hour) || 0 });
        },
        get pendingRepliesCount() {
            return this.replies.counts?.pending ?? this.activePage?.pending_replies ?? 0;
        },
        replyStatusLabel(status) {
            return { pending: 'Menunggu persetujuan', replying: 'Sedang dikirim', replied: 'Terkirim',
                     skipped: 'Dilewati', failed: 'Gagal', dismissed: 'Diabaikan' }[status] || status;
        },
        replyBadgeClass(status) {
            if (status === 'replied') return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
            if (status === 'failed') return 'bg-rose-500/10 text-rose-300 border-rose-500/30';
            if (status === 'replying') return 'bg-sky-500/10 text-sky-300 border-sky-500/30';
            if (status === 'pending') return 'bg-gold-500/10 text-gold-300 border-gold-500/30';
            return 'bg-slate-500/10 text-slate-300 border-slate-500/30';
        },
        get replyStatusText() {
            const p = this.activePage;
            if (!p) return 'Pilih Fanspage terlebih dahulu.';
            if (!p.auto_reply_enabled) return 'Nonaktif — komentar hanya diperiksa saat Anda menekan tombol.';
            if (!p.has_token) return 'Tidak berjalan — Access Token kosong.';
            const mode = p.auto_reply_mode === 'review' ? 'membuat draft untuk disetujui' : 'membalas otomatis';
            const next = this.schedule.next_reply_scan ? ` · berikutnya ${this.formatDate(this.schedule.next_reply_scan)}` : '';
            return `Aktif — ${mode} tiap ${this.schedule.reply_interval_minutes || 10} menit${next}.`;
        },

        async fetchReplies(more = false) {
            if (!this.activePageId) { this.replies = { items: [], total: 0, counts: {}, has_more: false }; return; }
            this.isLoadingReplies = true;
            const offset = more ? this.replies.items.length : 0;
            try {
                const data = await this.scopedData('replies',
                    `/api/replies?page=${this.activePageId}&status=${encodeURIComponent(this.replyStatus)}&limit=30&offset=${offset}`);
                if (!data) return;
                const items = data.items.map(r => ({ ...r, draft: r.reply_message, busy: false }));
                this.replies = { ...data, items: more ? [...this.replies.items, ...items] : items };
            } finally {
                this.isLoadingReplies = false;
            }
        },

        async saveReplySettings() {
            const page = this.activePage;
            if (!page || this.isSavingReplySettings) return;
            this.isSavingReplySettings = true;
            try {
                const res = await fetch(`/api/pages/${page.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...this.replyForm, reply_max_per_hour: parseInt(this.replyForm.reply_max_per_hour) || 20 })
                });
                const data = await res.json();
                if (!data.success) { this.showToast(data.message, 'error'); return; }
                Object.assign(page, this.replyFormFrom(data.page));
                this.syncReplyForm();
                this.showToast(page.auto_reply_enabled ? 'Balas komentar otomatis diaktifkan.' : 'Pengaturan balasan disimpan.');
            } catch (err) {
                this.showToast('Gagal menyimpan pengaturan balasan.', 'error');
            } finally {
                this.isSavingReplySettings = false;
            }
        },

        askScanReplies() {
            const page = this.activePage;
            if (!page) return;
            if (this.replyFormDirty) { this.showToast('Simpan pengaturan balasan terlebih dahulu.', 'error'); return; }
            if (page.auto_reply_mode === 'review') { this.scanReplies(); return; }
            this.askConfirm({
                title: 'Periksa dan balas sekarang?',
                message: `Komentar baru di "${page.name}" akan langsung dibalas di Facebook atas nama Fanspage ` +
                         `(paling banyak ${page.reply_max_per_hour} balasan per jam). ` +
                         'Pilih mode "Perlu persetujuan" bila ingin memeriksa balasan dulu.',
                confirmLabel: 'Ya, Balas Sekarang',
                tone: 'primary',
                action: () => this.scanReplies()
            });
        },

        async scanReplies() {
            if (this.isScanningReplies || !this.activePage) return;
            const page = this.activePage;
            this.isScanningReplies = true;
            try {
                const res = await fetch(`/api/replies/scan?page=${page.id}`, { method: 'POST' });
                const data = await res.json();
                this.showToast(data.message || 'Selesai.', data.success ? 'success' : 'error');
                await Promise.all([this.fetchReplies(), this.fetchPages()]);
            } catch (err) {
                this.showToast('Gagal memeriksa komentar.', 'error');
            } finally {
                this.isScanningReplies = false;
            }
        },

        async replyAction(item, action) {
            if (item.busy) return;
            if (action === 'send' && !(item.draft || '').trim()) { this.showToast('Balasan tidak boleh kosong.', 'error'); return; }
            item.busy = true;
            try {
                const res = await fetch(`/api/replies/${item.id}/${action}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: action === 'send' ? JSON.stringify({ message: item.draft }) : '{}'
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Permintaan ditolak.');
                if (data.reply) Object.assign(item, data.reply, { draft: data.reply.reply_message });
                this.showToast(data.message, data.success ? 'success' : 'error');
                if (data.success && action !== 'regenerate') await Promise.all([this.fetchReplies(), this.fetchPages()]);
            } catch (err) {
                this.showToast(err.message, 'error');
            } finally {
                item.busy = false;
            }
        },

        showGeminiKey: false,
        showOpenaiKey: false,
        maskCleared: {},

        // Settings State
        settings: {
            gemini_api_key: '',
            gemini_text_model: 'gemini-3.8-flash',
            gemini_image_model: 'gemini-3.1-flash-image',
            openai_api_key: '',
            openai_text_model: 'gpt-6.1-sol',
            openai_image_model: 'gpt-image-2.5-flare',
            text_reasoning: 'low',
            openai_image_quality: 'medium',
            text_provider: 'gemini',
            image_provider: 'gemini',
            auto_topic_evolution: 'true',
            topic_window_days: '7',
            max_new_topics_per_cycle: '2'
        },
        settingsDirty: false,
        schedule: { enabled: false, timezone: '', pages: [], next_run: null, next_evolution: null },

        // Fanspage management
        pages: [],
        activePageId: null,
        verifyingPageId: null,
        addPage: { show: false, page_id: '', access_token: '', busy: false, error: '', preview: null },

        // Topics & Generation State
        topics: [],
        showInactiveTopics: false,
        selectedTopicId: 'auto',

        // Weekly trend & topic evolution
        windowStats: { window_days: 7, posts_in_window: 0, total_reach: 0, ranking: [] },
        windowDays: 7,
        evolveResult: null,
        isEvolving: false,
        isLoadingWindow: false,
        currentPost: null,
        postsList: [],
        postsTotal: 0,
        postsHasMore: false,
        postsPerPage: 30,
        isLoadingMore: false,
        editorOpen: false,   // caption editor panel under the topic picker, folded by default
        // Home feed under the Studio: every generated post, newest first, loaded in
        // small batches as the user scrolls down. 'all' = every Fanspage.
        feed: { items: [], hasMore: true, loading: false, error: '', scope: 'all', total: 0, expanded: {} },
        feedPerPage: 10,
        feedRequestId: 0,
        analyticsSummary: {
            total_posts: 0,
            total_reach: 0,
            total_engagement: 0,
            winning_topic: 'Menganalisis...'
        },

        // Loading indicators
        isGenerating: false,
        genStep: 0,
        elapsedLabel: '00:00',
        isPublishing: false,
        isSaving: false,
        isSyncing: false,
        isOptimizing: false,
        isTestingGemini: false,
        isTestingOpenai: false,


        // Overlays
        toast: { show: false, message: '', type: 'success' },
        toastTimer: null,
        modalImage: { show: false, url: '' },
        confirmDialog: {
            show: false,
            title: '',
            message: '',
            preview: '',
            confirmLabel: 'Lanjutkan',
            tone: 'primary',
            action: null
        },

        // ---------- lifecycle ----------
        async initApp() {
            this.installDialogFocus();
            window.addEventListener('beforeunload', e => {
                if (this.hasUnsavedChanges && !this.loggingOut) { e.preventDefault(); e.returnValue = ''; }
            });
            this.fetchMe();
            await this.fetchSettings();
            this.windowDays = parseInt(this.settings.topic_window_days) || 7;
            // Pages first: every other request is scoped to the active page.
            await this.fetchPages();
            this.loadedPageId = this.activePageId;
            this.syncReplyForm();
            await Promise.all([
                this.fetchTopics(),
                this.fetchPosts(),
                this.fetchAnalytics(),
                this.fetchScheduleStatus(),
                this.fetchWindowStats()
            ]);
            if (this.pages.length === 0) {
                this.activeTab = 'pages';   // nothing works without a Fanspage
            }
            this.installFeedObserver();
        },

        // ---------- fanspage management ----------
        installDialogFocus() {
            let open = null, previous = null;
            const sync = () => {
                const dialog = [...document.querySelectorAll('[role="dialog"]')].find(el => getComputedStyle(el).display !== 'none');
                if (dialog === open) return;
                document.querySelectorAll('header, main').forEach(el => { el.inert = !!dialog; });
                document.body.style.overflow = dialog ? 'hidden' : '';
                if (dialog) {
                    if (!open) previous = document.activeElement;
                    open = dialog;
                    dialog.querySelector('input, button, [tabindex]')?.focus();
                } else {
                    open = null;
                    previous?.focus();
                }
            };
            new MutationObserver(sync).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['style']});
            document.addEventListener('keydown', e => {
                if (!open || e.key !== 'Tab') return;
                const items = [...open.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
                    .filter(el => !el.disabled && el.offsetParent !== null && el.tabIndex >= 0);
                if (!items.length) return;
                const first = items[0], last = items[items.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
            });
        },
        get activePage() {
            return this.pages.find(p => p.id === this.activePageId) || null;
        },

        async fetchPages() {
            try {
                const res = await fetch('/api/pages');
                if (!res.ok) throw new Error('Gagal memuat Fanspage.');
                const data = await res.json();
                this.pages = (data.pages || []).map(p => {
                    const previous = this.pages.find(old => old.id === p.id);
                    return previous?.dirty ? previous : {...p, newToken: previous?.newToken || ''};
                });
                delete this.dataErrors.pages;
                // Keep the active selection valid after add/delete.
                if (!this.pages.some(p => p.id === this.activePageId)) {
                    this.activePageId = this.pages.length ? this.pages[0].id : null;
                }
            } catch (err) {
                console.error('Error loading pages:', err);
                this.dataErrors.pages = 'Gagal memuat Fanspage.';
            }
        },

        // Everything in Studio & Analitik is scoped to the selected page.
        async onPageSwitch() {
            const next = this.activePageId;
            this.activePageId = this.loadedPageId;
            this.leaveDraft(async () => {
                this.activePageId = next;
                this.loadedPageId = next;
                this.currentPost = null;
                this.postsList = []; this.topics = [];
                this.analyticsSummary = {total_posts: 0, winning_topic: 'Memuat…'};
                this.windowStats = {ranking: []};
                this.selectedTopicId = 'auto';
                this.isLoadingPage = true;
                this.replies = { items: [], total: 0, counts: {}, has_more: false };
                this.syncReplyForm();
                await Promise.all([this.fetchPosts(), this.fetchAnalytics(), this.fetchWindowStats(), this.fetchTopics(), this.fetchReplies()]);
                if (this.activePageId === next) this.isLoadingPage = false;
            });
        },

        pageQuery(prefix = '?') {
            return this.activePageId ? `${prefix}page=${this.activePageId}` : '';
        },

        nextRunForPage(pageRowId) {
            const entry = (this.schedule.pages || []).find(p => p.page_id === pageRowId);
            if (!entry || !entry.next_run) return 'menunggu jadwal';
            return `${entry.times.join(', ')} · berikutnya ${this.formatDate(entry.next_run)}`;
        },

        autopilotStatus(page) {
            if (page.dirty) return 'Perubahan belum disimpan';
            if (!page.is_active) return 'Dijeda — halaman tidak aktif';
            if (!page.autopilot_enabled) return 'Nonaktif — publish manual saja';
            if (!page.has_token) return 'Tidak berjalan — Access Token kosong';
            if (this.aiMissingKey) return `Belum siap — isi ${this.aiMissingKey} API Key`;
            return `Dijadwalkan — ${this.nextRunForPage(page.id)}. Periksa koneksi sebelum menjalankan.`;
        },

        openAddPage() {
            this.addPage = { show: true, page_id: '', access_token: '', busy: false, error: '', preview: null };
        },

        async verifyNewPage() {
            if (!this.addPage.page_id || !this.addPage.access_token) {
                this.addPage.error = 'Page ID dan Access Token wajib diisi.';
                return;
            }
            this.addPage.busy = true;
            this.addPage.error = '';
            this.addPage.preview = null;
            try {
                const res = await fetch('/api/pages/verify', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        page_id: this.addPage.page_id,
                        access_token: this.addPage.access_token
                    })
                });
                const data = await res.json();
                if (data.success) {
                    this.addPage.preview = data;
                } else {
                    this.addPage.error = data.message;
                }
            } catch (err) {
                this.addPage.error = 'Gagal menghubungi server.';
            } finally {
                this.addPage.busy = false;
            }
        },

        async submitNewPage() {
            if (!this.addPage.page_id || !this.addPage.access_token) {
                this.addPage.error = 'Page ID dan Access Token wajib diisi.';
                return;
            }
            this.addPage.busy = true;
            this.addPage.error = '';
            try {
                const res = await fetch('/api/pages', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        page_id: this.addPage.page_id,
                        access_token: this.addPage.access_token,
                        verify: true
                    })
                });
                const data = await res.json();
                if (data.success) {
                    this.addPage.show = false;
                    await this.fetchPages();
                    if (data.page) this.activePageId = data.page.id;
                    await Promise.all([this.fetchScheduleStatus(), this.onPageSwitch()]);
                    this.showToast(data.message);
                } else {
                    this.addPage.error = data.message;
                }
            } catch (err) {
                this.addPage.error = 'Gagal menambahkan Fanspage.';
            } finally {
                this.addPage.busy = false;
            }
        },

        async savePage(page) {
            if (this.savingPageId) return;
            this.savingPageId = page.id;
            try {
                const before = JSON.stringify([page.content_language, page.aspect_ratio, page.color_theme, page.auto_post_times, page.autopilot_enabled, page.is_active]);
                const res = await fetch(`/api/pages/${page.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: page.name,
                        content_language: page.content_language,
                        aspect_ratio: page.aspect_ratio,
                        color_theme: page.color_theme,
                        auto_post_times: page.auto_post_times,
                        autopilot_enabled: page.autopilot_enabled,
                        is_active: page.is_active
                    })
                });
                const data = await res.json();
                if (data.success) {
                    page.dirty = before !== JSON.stringify([page.content_language, page.aspect_ratio, page.color_theme, page.auto_post_times, page.autopilot_enabled, page.is_active]);
                    await this.fetchScheduleStatus();
                    this.showToast(`Pengaturan '${page.name}' disimpan.`);
                } else {
                    this.showToast(data.message, 'error');
                    page.dirty = true;
                }
            } catch (err) {
                this.showToast('Gagal menyimpan pengaturan Fanspage.', 'error');
            } finally { this.savingPageId = null; }
        },

        async replaceToken(page) {
            if (!page.newToken) {
                this.showToast('Tempel token barunya terlebih dahulu.', 'error');
                return;
            }
            try {
                const res = await fetch(`/api/pages/${page.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ access_token: page.newToken })
                });
                const data = await res.json();
                if (data.success) {
                    page.newToken = '';
                    await this.reverifyPage(page);
                } else {
                    this.showToast(data.message, 'error');
                }
            } catch (err) {
                this.showToast('Gagal mengganti token.', 'error');
            }
        },

        async reverifyPage(page) {
            this.verifyingPageId = page.id;
            try {
                const res = await fetch(`/api/pages/${page.id}/verify`, { method: 'POST' });
                const data = await res.json();
                this.showToast(data.message, data.success ? 'success' : 'error');
                await this.fetchPages();
            } catch (err) {
                this.showToast('Gagal memverifikasi Fanspage.', 'error');
            } finally {
                this.verifyingPageId = null;
            }
        },

        askDeletePage(page) {
            this.askConfirm({
                title: 'Hapus Fanspage ini?',
                message: `"${page.name}" akan dihapus beserta pembelajaran topiknya. ` +
                         (page.published_count > 0
                            ? `${page.published_count} postingan yang sudah tayang tetap tersimpan sebagai riwayat dan tidak dihapus dari Facebook.`
                            : 'Belum ada postingan yang tayang dari halaman ini.'),
                confirmLabel: 'Hapus Fanspage',
                tone: 'danger',
                action: () => this.deletePage(page)
            });
        },

        async deletePage(page) {
            try {
                const res = await fetch(`/api/pages/${page.id}`, { method: 'DELETE' });
                const data = await res.json();
                if (data.success) {
                    await this.fetchPages();
                    await Promise.all([this.fetchScheduleStatus(), this.onPageSwitch()]);
                    this.showToast(data.message);
                } else {
                    this.showToast(data.message, 'error');
                }
            } catch (err) {
                this.showToast('Gagal menghapus Fanspage.', 'error');
            }
        },

        // The Fanspage a post belongs to. A draft still being created has no page
        // of its own yet, so it follows the header selection.
        postPage(post) {
            if (!post) return null;
            const id = post.page_id || (post.id ? null : this.activePageId);
            return this.pages.find(p => p.id === (id ?? this.activePageId)) || null;
        },

        // ---------- derived ----------
        // The seed taxonomy is the permanent base curriculum; AI topics are the
        // variants the learning loop grows on top of it.
        get baseTopics() {
            return this.topics.filter(t => t.is_base);
        },

        get autopilotPagesCount() {
            return this.pages.filter(p => p.autopilot_enabled && p.is_active).length;
        },

        get variantTopics() {
            return this.topics.filter(t => !t.is_base);
        },

        // ---------- helpers ----------
        showToast(msg, type = 'success') {
            this.toast.message = msg;
            this.toast.type = type;
            this.toast.show = true;
            clearTimeout(this.toastTimer);
            // Errors stay longer: they usually carry a message worth reading.
            this.toastTimer = setTimeout(() => { this.toast.show = false; }, type === 'error' ? 8000 : 4000);
        },

        // Always land at the top of the tab you just opened, instead of keeping the
        // scroll offset of the previous (usually much longer) tab.
        switchTab(tab) {
            if (this.activeTab === 'generator' && tab !== 'generator' && this.draftDirty) {
                this.askConfirm({title: 'Draft belum disimpan', message: 'Simpan draft atau lanjutkan. Perubahan tetap tersedia selama Anda belum mengganti konten atau menutup halaman.', confirmLabel: 'Lanjutkan', tone: 'primary', action: () => { this.activeTab = tab; window.scrollTo(0, 0); }});
                return;
            }
            if (this.activeTab !== tab) {
                this.activeTab = tab;
                this.$nextTick(() => window.scrollTo({ top: 0, behavior: 'smooth' }));
            }
            if (tab === 'replies') {
                if (!this.replyFormDirty) this.syncReplyForm();
                this.fetchReplies();
                this.fetchScheduleStatus();
            }
        },

        closeOverlays() {
            this.modalImage.show = false;
            this.confirmDialog.show = false;
            this.addPage.show = false;
        },

        openImageModal(url) {
            this.modalImage.url = url;
            this.modalImage.show = true;
        },

        formatDate(isoString) {
            if (!isoString) return '-';
            const date = new Date(isoString);
            if (isNaN(date)) return '-';
            return date.toLocaleString('id-ID', {
                day: 'numeric',
                month: 'short',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        },

        statusLabel(status) {
            return {
                published: 'Tayang',
                publishing: 'Sedang Dipublikasikan',
                ready: 'Siap Publish',
                draft: 'Draft',
                failed: 'Gagal'
            }[status] || status;
        },

        statusBadgeClass(status) {
            if (status === 'published') return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
            if (status === 'failed') return 'bg-rose-500/10 text-rose-300 border-rose-500/30';
            if (status === 'publishing') return 'bg-sky-500/10 text-sky-300 border-sky-500/30';
            return 'bg-gold-500/10 text-gold-300 border-gold-500/30';
        },

        categoryClass(category) {
            return {
                Fluvial: 'bg-blue-500/10 text-blue-300 border-blue-500/30',
                Geology: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
                Minerals: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
                Strategy: 'bg-purple-500/10 text-purple-300 border-purple-500/30',
                Equipment: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30',
                'Rules & Value': 'bg-rose-500/10 text-rose-300 border-rose-500/30'
            }[category] || 'bg-slate-700/40 text-slate-300 border-slate-600/40';
        },

        // Clear the mask as soon as the user focuses the field, so they type a
        // fresh secret instead of appending to the placeholder dots.
        clearMaskOnEdit(key) {
            if (this.settings[key] === SECRET_MASK) {
                this.settings[key] = '';
                this.maskCleared[key] = true;
            }
        },

        // Left empty without typing: put the mask back so saving keeps the stored secret.
        restoreMaskIfEmpty(key) {
            if (this.maskCleared[key] && !this.settings[key]) this.settings[key] = SECRET_MASK;
            delete this.maskCleared[key];
        },

        isSecretStored(key) { return this.settings[key] === SECRET_MASK; },

        async copyCaption() {
            try {
                await navigator.clipboard.writeText(this.currentPost.caption || '');
                this.showToast('Caption disalin ke clipboard.');
            } catch (err) {
                this.showToast('Browser menolak akses clipboard.', 'error');
            }
        },

        // ---------- confirmation flow ----------
        askConfirm({ title, message, preview = '', confirmLabel = 'Lanjutkan', tone = 'primary', action }) {
            this.confirmDialog = { show: true, title, message, preview, confirmLabel, tone, action };
        },

        runConfirmAction() {
            const action = this.confirmDialog.action;
            // Replace the whole object (guaranteed reactive) and let Alpine paint the
            // close before the action starts, so the dialog can't linger on screen.
            this.confirmDialog = { ...this.confirmDialog, show: false, action: null };
            if (typeof action === 'function') {
                this.$nextTick(() => action());
            }
        },

        askPublish(post) {
            if (!post) return;
            // Publish to the post's own page; fall back to the one selected in the header.
            const target = this.pages.find(p => p.id === (post.page_id || this.activePageId));
            if (!target) {
                this.showToast('Belum ada Fanspage terdaftar. Tambahkan dulu di tab Fanspage.', 'error');
                this.switchTab('pages');
                return;
            }
            if (!target.has_token) {
                this.showToast(`Fanspage '${target.name}' belum punya Access Token.`, 'error');
                this.switchTab('pages');
                return;
            }
            this.askConfirm({
                title: 'Publikasikan ke Facebook?',
                message: `"${post.visual_title}" akan langsung tayang di ${target.name} dan bisa dilihat publik. `
                       + (post.is_orphan ? 'Draft ini kehilangan Fanspage asalnya, jadi akan diposting ke halaman yang sedang dipilih di header. ' : '')
                       + 'Tindakan ini tidak bisa dibatalkan dari sini.',
                preview: (post.caption || post.caption_preview || '').slice(0, 280),
                confirmLabel: 'Ya, Publish Sekarang',
                tone: 'primary',
                action: () => this.publishToFacebook(post)
            });
        },

        askDelete(post) {
            if (!post) return;
            this.askConfirm({
                title: 'Hapus draft ini?',
                message: `Draft "${post.visual_title}" beserta file posternya akan dihapus permanen.`,
                confirmLabel: 'Hapus Permanen',
                tone: 'danger',
                action: () => this.deletePost(post)
            });
        },

        // ---------- login account ----------
        async fetchMe() {
            try {
                const res = await fetch('/api/auth/me');
                if (res.ok) this.authEmail = (await res.json()).email || '';
            } catch (err) { /* the header simply shows no email */ }
        },

        logout() {
            const go = async () => {
                this.loggingOut = true;
                try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (err) { /* cookie is dropped by the login page anyway */ }
                window.location.replace('/login');
            };
            if (!this.hasUnsavedChanges) { go(); return; }
            this.askConfirm({ title: 'Ada perubahan yang belum disimpan',
                message: 'Keluar sekarang akan membuang perubahan tersebut.',
                confirmLabel: 'Keluar', tone: 'danger', action: go });
        },

        async changePassword() {
            const form = this.passwordForm;
            if (form.next.length < 8) { this.showToast('Password baru minimal 8 karakter.', 'error'); return; }
            if (form.next !== form.confirm) { this.showToast('Ulangan password baru tidak sama.', 'error'); return; }
            this.isChangingPassword = true;
            try {
                const res = await fetch('/api/auth/change-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_password: form.current, new_password: form.next })
                });
                const data = await res.json();
                if (!data.success) throw new Error(data.message || 'Gagal mengganti password.');
                this.passwordForm = { current: '', next: '', confirm: '' };
                this.showToast(data.message);
            } catch (err) {
                this.showToast(err.message || 'Gagal menghubungi server.', 'error');
            } finally {
                this.isChangingPassword = false;
            }
        },

        // ---------- settings ----------
        async fetchSettings() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                this.settings = { ...this.settings, ...data };
                this.settingsDirty = false;
                // The server remembers successful checks, so the status survives a reload.
                for (const p of ['gemini', 'openai']) {
                    this.recordCheck(p, data[`${p}_status`] || 'Belum diverifikasi', this.providerSnapshot(p));
                }
            } catch (err) {
                console.error('Error loading settings:', err);
                this.showToast('Gagal memuat pengaturan dari server.', 'error');
            }
        },

        async saveSettings(options = {}) {
            if (this.isSaving) return false;
            const { silent = false } = options;
            this.isSaving = true;
            try {
                const snapshot = JSON.stringify(this.settings);
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.settings)
                });
                const data = await res.json();
                if (data.success) {
                    this.settingsDirty = snapshot !== JSON.stringify(this.settings);
                    if (data.scheduler) this.schedule = data.scheduler;
                    if (!silent) this.showToast('Pengaturan berhasil disimpan.');
                    return true;
                }
                this.showToast(data.message || 'Gagal menyimpan pengaturan.', 'error');
                return false;
            } catch (err) {
                this.showToast('Gagal terhubung ke server.', 'error');
                return false;
            } finally {
                this.isSaving = false;
            }
        },

        async fetchScheduleStatus() {
            try {
                const res = await fetch('/api/scheduler/status');
                if (!res.ok) throw new Error('Gagal memuat jadwal');
                this.schedule = await res.json();
                delete this.dataErrors.schedule;
            } catch (err) {
                console.error('Error loading scheduler status:', err);
                this.dataErrors.schedule = 'Gagal memuat jadwal';
            }
        },

        async testGeminiConnection() {
            if (!this.settings.gemini_api_key) {
                this.showToast('Masukkan Gemini API Key terlebih dahulu!', 'error');
                return;
            }
            this.isTestingGemini = true;
            const tested = this.providerSnapshot('gemini');
            try {
                const res = await fetch('/api/settings/test-gemini', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        api_key: this.settings.gemini_api_key,
                        model_name: this.settings.gemini_text_model
                    })
                });
                const data = await res.json();
                this.recordCheck('gemini', data.success ? 'Terhubung' : 'Gagal', tested);
                this.showToast(data.message, data.success ? 'success' : 'error');
            } catch (err) {
                this.recordCheck('gemini', 'Gagal', tested);
                this.showToast('Gagal mengetes Gemini API.', 'error');
            } finally {
                this.isTestingGemini = false;
            }
        },

        async testOpenaiConnection() {
            if (!this.settings.openai_api_key) {
                this.showToast('Masukkan OpenAI API Key terlebih dahulu!', 'error');
                return;
            }
            this.isTestingOpenai = true;
            const tested = this.providerSnapshot('openai');
            try {
                const res = await fetch('/api/settings/test-openai', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        api_key: this.settings.openai_api_key,
                        text_model: this.settings.openai_text_model,
                        image_model: this.settings.openai_image_model
                    })
                });
                const data = await res.json();
                this.recordCheck('openai', data.success ? 'Terhubung' : 'Gagal', tested);
                this.showToast(data.message, data.success ? 'success' : 'error');
            } catch (err) {
                this.recordCheck('openai', 'Gagal', tested);
                this.showToast('Gagal mengetes OpenAI API.', 'error');
            } finally {
                this.isTestingOpenai = false;
            }
        },

        // ---------- data ----------
        async fetchTopics() {
            try {
                const data = await this.scopedData('topics', `/api/topics?include_inactive=${this.showInactiveTopics}${this.pageQuery('&')}`);
                if (data) this.topics = data;
            } catch (err) {
                console.error('Error loading topics:', err);
            }
        },

        // ---------- weekly trend & topic evolution ----------
        async fetchWindowStats(days) {
            if (days) this.windowDays = days;
            this.isLoadingWindow = true;
            try {
                const data = await this.scopedData('window', `/api/analytics/window?days=${this.windowDays}${this.pageQuery('&')}`);
                if (data) this.windowStats = data;
            } catch (err) {
                console.error('Error loading window stats:', err);
            } finally {
                this.isLoadingWindow = false;
            }
        },

        askEvolveTopics() {
            const top = this.windowStats.ranking?.[0];
            if (!top) {
                this.showToast(
                    `Belum ada postingan dengan metrik dalam ${this.windowDays} hari terakhir. ` +
                    'Publikasikan konten lalu klik "Refresh Metrik Facebook".', 'error');
                return;
            }
            this.askConfirm({
                title: 'Buat topik baru dari pemenang?',
                message: `AI akan mempelajari ${this.windowStats.ranking.length} topik yang tayang dalam ` +
                         `${this.windowDays} hari terakhir — dipimpin "${top.topic_title}" dengan jangkauan ` +
                         `${top.reach.toLocaleString('id-ID')} — lalu merancang topik turunan yang baru. ` +
                         `Proses ini memakai kuota ${this.providerLabel(this.settings.text_provider)} API Anda.`,
                confirmLabel: 'Ya, Buat Topik Baru',
                tone: 'primary',
                action: () => this.evolveTopics()
            });
        },

        async evolveTopics() {
            this.isEvolving = true;
            this.evolveResult = null;
            try {
                const res = await fetch(`/api/topics/evolve${this.pageQuery()}`, { method: 'POST' });
                const data = await res.json();
                this.evolveResult = data;
                if (data.success) {
                    this.showToast(data.message);
                    await Promise.all([this.fetchTopics(), this.fetchAnalytics()]);
                } else {
                    this.showToast(data.message, 'error');
                }
            } catch (err) {
                this.showToast('Gagal membuat topik baru.', 'error');
            } finally {
                this.isEvolving = false;
            }
        },

        askRetireTopic(topic) {
            if (topic.is_base) {
                this.showToast(
                    'Topik dasar adalah kurikulum fondasi dan tidak bisa dinonaktifkan. ' +
                    'Bobotnya akan turun sendiri bila kurang diminati.', 'error');
                return;
            }
            this.askConfirm({
                title: 'Nonaktifkan topik turunan ini?',
                message: `"${topic.title}" tidak akan dipilih lagi untuk konten baru. ` +
                         'Riwayat postingannya tetap tersimpan dan topik bisa diaktifkan kembali kapan saja.',
                confirmLabel: 'Nonaktifkan',
                tone: 'danger',
                action: () => this.setTopicActive(topic, false)
            });
        },

        async setTopicActive(topic, active) {
            try {
                const action = active ? 'reactivate' : 'retire';
                const res = await fetch(`/api/topics/${topic.id}/${action}`, { method: 'POST' });
                const data = await res.json();
                this.showToast(data.message, data.success ? 'success' : 'error');
                if (data.success) await this.fetchTopics();
            } catch (err) {
                this.showToast('Gagal mengubah status topik.', 'error');
            }
        },

        async fetchPosts({ append = false } = {}) {
            try {
                const offset = append ? this.postsList.length : 0;
                const sep = this.activePageId ? '&' : '?';
                const data = await this.scopedData('posts', `/api/posts${this.pageQuery()}${sep}limit=${this.postsPerPage}&offset=${offset}&search=${encodeURIComponent(this.historySearch)}&status=${encodeURIComponent(this.historyStatus)}`);
                if (!data) return;
                this.postsList = append ? [...this.postsList, ...data.items] : data.items;
                if (!append) this.resetFeed();
                this.postsTotal = data.total;
                this.postsHasMore = data.has_more;
                // The list is a summary; the Studio needs the full record.
                if (this.postsList.length > 0 && !this.currentPost) {
                    await this.openInStudio(this.postsList[0], { silent: true });
                }
            } catch (err) {
                console.error('Error loading posts:', err);
            }
        },

        // ---------- home feed (infinite scroll) ----------
        feedUrl(offset) {
            const page = this.feed.scope === 'page' && this.activePageId ? `&page=${this.activePageId}` : '';
            return `/api/posts?with_caption=true&limit=${this.feedPerPage}&offset=${offset}${page}`;
        },
        setFeedScope(scope) {
            if (this.feed.scope === scope) return;
            this.feed.scope = scope;
            this.resetFeed();
        },
        resetFeed() {
            this.feedRequestId++;   // a batch still in flight belongs to the old list
            this.feed = { ...this.feed, items: [], hasMore: true, loading: false, error: '', expanded: {} };
            this.loadFeed();
        },
        async loadFeed() {
            if (this.feed.loading || !this.feed.hasMore) return;
            const id = this.feedRequestId;
            this.feed.loading = true;
            this.feed.error = '';
            try {
                const res = await fetch(this.feedUrl(this.feed.items.length));
                if (!res.ok) throw new Error('Gagal memuat postingan.');
                const data = await res.json();
                if (id !== this.feedRequestId) return;
                // A post generated meanwhile shifts the offsets by one: skip repeats.
                const seen = new Set(this.feed.items.map(p => p.id));
                this.feed.items = [...this.feed.items, ...data.items.filter(p => !seen.has(p.id))];
                this.feed.total = data.total;
                this.feed.hasMore = data.has_more && data.items.length > 0;
            } catch (err) {
                if (id === this.feedRequestId) this.feed.error = err.message || 'Gagal memuat postingan.';
            } finally {
                if (id === this.feedRequestId) {
                    this.feed.loading = false;
                    // A tall screen can still show the bottom after a batch: keep going.
                    if (!this.feed.error) this.$nextTick(() => { if (this.feedSentinelVisible()) this.loadFeed(); });
                }
            }
        },
        feedSentinelVisible() {
            const el = this.$refs?.feedSentinel;
            if (!el || this.activeTab !== 'generator' || typeof window === 'undefined') return false;
            const box = el.getBoundingClientRect();
            return box.height > 0 && box.top < window.innerHeight + 600;
        },
        installFeedObserver() {
            const el = this.$refs?.feedSentinel;
            if (!el || typeof IntersectionObserver === 'undefined') return;
            new IntersectionObserver(entries => {
                if (entries.some(e => e.isIntersecting) && this.activeTab === 'generator') this.loadFeed();
            }, { rootMargin: '0px 0px 600px 0px' }).observe(el);
        },
        patchFeedItem(post) {
            const i = this.feed.items.findIndex(p => p.id === post.id);
            if (i >= 0) this.feed.items[i] = { ...this.feed.items[i], ...post };
        },
        // The post open in the editor is shown with its live (possibly unsaved) edits.
        get feedPosts() {
            const open = this.currentPost;
            return this.feed.items.map(p => (open && open.id === p.id ? { ...p, ...open } : p));
        },
        feedPage(post) {
            return this.pages.find(p => p.id === post.page_id) || null;
        },

        async loadMorePosts() {
            this.isLoadingMore = true;
            try {
                await this.fetchPosts({ append: true });
            } finally {
                this.isLoadingMore = false;
            }
        },

        async fetchAnalytics() {
            try {
                const data = await this.scopedData('analytics', `/api/analytics/summary${this.pageQuery()}`);
                if (data) this.analyticsSummary = data;
            } catch (err) {
                console.error('Error loading analytics:', err);
            }
        },

        // ---------- live Facebook numbers ----------
        fmtCount(n) { return (Number(n) || 0).toLocaleString('id-ID'); },
        metricsStale(post) {
            if (!post || post.status !== 'published') return false;
            const checked = post.metrics?.last_checked_at;
            return !checked || Date.now() - new Date(checked).getTime() > 10 * 60 * 1000;
        },
        async refreshPostMetrics(post, { silent = false } = {}) {
            if (!post || this.isRefreshingMetrics) return;
            this.isRefreshingMetrics = true;
            try {
                const res = await fetch(`/api/posts/${post.id}/metrics/refresh`, { method: 'POST' });
                const data = await res.json();
                if (!data.success) throw new Error(data.message || data.detail || 'Gagal mengambil angka dari Facebook.');
                if (this.currentPost?.id === post.id) this.currentPost.metrics = data.metrics;
                this.patchFeedItem({id: post.id, metrics: data.metrics});
                if (!silent) this.showToast('Angka terbaru dari Facebook dimuat.');
            } catch (err) {
                if (!silent) this.showToast(err.message || 'Gagal menghubungi server.', 'error');
            } finally {
                this.isRefreshingMetrics = false;
            }
        },

        async openInStudio(post, { silent = false, discard = false } = {}) {
            if (this.draftDirty && !discard) {
                this.leaveDraft(() => this.openInStudio(post, {silent, discard: true})); return;
            }
            try {
                // The list omits caption/prompt to stay small: fetch the full record.
                const data = await this.scopedData('detail', `/api/posts/${post.id}`);
                if (!data) return;
                this.acceptPost(data);
            } catch (err) {
                this.showToast('Gagal memuat isi postingan.', 'error');
                return;
            }
            // Live post: show today's numbers, not last night's snapshot.
            if (this.metricsStale(this.currentPost)) this.refreshPostMetrics(this.currentPost, { silent: true });
            if (!silent) {
                this.editorOpen = true;   // opened on purpose: the user wants to edit it
                this.switchTab('generator');
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        },

        // ---------- generation ----------
        async generateContent(discard = false) {
            if (this.isGenerating || !this.readyToGenerate) return;
            if (this.draftDirty && !discard) { this.leaveDraft(() => this.generateContent(true)); return; }
            const originalPage = this.activePageId;
            if (this.aiMissingKey) {
                this.showToast(`Harap isi ${this.aiMissingKey} API Key di tab Pengaturan terlebih dahulu!`, 'error');
                this.switchTab('settings');
                return;
            }

            this.isGenerating = true;
            this.genStep = 1;

            const startedAt = Date.now();
            const ticker = setInterval(() => {
                const secs = Math.floor((Date.now() - startedAt) / 1000);
                this.elapsedLabel = `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
                // The server runs text generation first, then the image render.
                if (secs >= 8 && this.genStep < 2) this.genStep = 2;
            }, 1000);

            try {
                const res = await fetch('/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        topic_id: this.selectedTopicId,
                        page_id: this.activePageId
                    })
                });
                const data = await res.json();

                if (data.success) {
                    this.genStep = 4;
                    if (this.activePageId === originalPage) { this.acceptPost(data.post); this.editorOpen = true; }
                    await Promise.all([this.fetchPosts(), this.fetchTopics()]);
                    this.showToast('Konten infografis berhasil dibuat!');
                } else {
                    this.showToast(data.message || 'Gagal membuat konten.', 'error');
                }
            } catch (err) {
                this.showToast('Terjadi kesalahan saat generate konten.', 'error');
            } finally {
                clearInterval(ticker);
                this.isGenerating = false;
                this.genStep = 0;
                this.elapsedLabel = '00:00';
            }
        },

        async regenerateImage(postId) {
            if (this.isGenerating || this.isRegenerating) return;
            this.isRegenerating = true;
            try {
                const res = await fetch(`/api/posts/${postId}/regenerate-image`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    if (this.currentPost?.id === postId) {
                        this.currentPost.image_url = data.image_url;
                        this.currentPost.prompt_used = data.prompt_used;
                    }
                    this.showToast('Poster berhasil di-render ulang.');
                    await this.fetchPosts();
                } else {
                    this.showToast(data.message || 'Gagal render ulang gambar.', 'error');
                }
            } catch (err) {
                this.showToast('Gagal menghubungi server.', 'error');
            } finally {
                this.isRegenerating = false;
            }
        },

        captionLanguageLabel(post) {
            return (this.postPage(post)?.content_language || 'en') === 'id' ? 'Indonesia' : 'English';
        },

        // Unsaved edits would be overwritten by the new caption, so ask first.
        regenerateCaption(post) {
            if (this.isGenerating || this.isRewritingCaption) return;
            this.leaveDraft(() => this.runRegenerateCaption(post.id));
        },

        async runRegenerateCaption(postId) {
            this.isRewritingCaption = true;
            try {
                const res = await fetch(`/api/posts/${postId}/regenerate-caption`, { method: 'POST' });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.detail || data.message || 'Gagal membuat ulang caption.');
                if (this.currentPost?.id === postId) {
                    // Discarded edits go too: the title shown must be the one the server has.
                    const [savedTitle] = JSON.parse(this.draftSnapshot || '[]');
                    if (savedTitle !== undefined) this.currentPost.visual_title = savedTitle;
                    this.currentPost.caption = data.caption;
                    this.draftSnapshot = JSON.stringify([this.currentPost.visual_title, data.caption]);
                }
                this.showToast('Caption baru berhasil dibuat dan disimpan.');
                await this.fetchPosts();
            } catch (err) {
                this.showToast(err.message || 'Gagal menghubungi server.', 'error');
            } finally {
                this.isRewritingCaption = false;
            }
        },

        // ---------- publishing ----------
        async publishToFacebook(post) {
            this.isPublishing = true;
            try {
                // Always publish the caption belonging to THIS post. When the post is
                // the one open in the studio, its edited text is sent along.
                const editing = this.currentPost && this.currentPost.id === post.id;
                const target = this.pages.find(p => p.id === (post.page_id || this.activePageId));
                const payload = editing
                    ? { caption: this.currentPost.caption, visual_title: this.currentPost.visual_title }
                    // From the history list we send no caption: the server keeps the
                    // stored one, which is exactly the post's own text.
                    : {};
                // Name the destination explicitly so an orphaned draft can never be
                // redirected to some other Fanspage behind the user's back.
                if (target) payload.page_id = target.id;

                const res = await fetch(`/api/posts/${post.id}/publish`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();

                if (data.success) {
                    this.showToast('Berhasil diposting ke Fanspage Facebook!');
                    if (this.currentPost && this.currentPost.id === post.id) {
                        this.currentPost.status = 'published';
                        this.currentPost.fb_post_url = data.post_url;
                        this.currentPost.published_at = new Date().toISOString();
                    }
                    await Promise.all([this.fetchPosts(), this.fetchAnalytics()]);
                } else {
                    this.showToast(data.message || 'Gagal mempublikasikan ke Facebook.', 'error');
                    if (data.needs_page) this.switchTab('pages');
                    await this.fetchPosts();
                }
            } catch (err) {
                this.showToast('Gagal mempublikasikan ke Facebook.', 'error');
            } finally {
                this.isPublishing = false;
            }
        },

        async deletePost(post) {
            try {
                const res = await fetch(`/api/posts/${post.id}`, { method: 'DELETE' });
                const data = await res.json();
                if (data.success) {
                    if (this.currentPost && this.currentPost.id === post.id) {
                        this.currentPost = null;
                    }
                    await this.fetchPosts();
                    if (!this.currentPost && this.postsList.length > 0) {
                        this.currentPost = { ...this.postsList[0] };
                    }
                    this.showToast(data.message);
                } else {
                    this.showToast(data.message || 'Gagal menghapus draft.', 'error');
                }
            } catch (err) {
                this.showToast('Gagal menghubungi server.', 'error');
            }
        },

        // ---------- analytics ----------
        async refreshAllMetrics() {
            this.isSyncing = true;
            try {
                const res = await fetch(`/api/analytics/sync${this.pageQuery()}`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    this.showToast(`Metrik ${data.updated_count} postingan diperbarui.`);
                    await Promise.all([this.fetchPosts(), this.fetchAnalytics(), this.fetchWindowStats()]);
                } else {
                    this.showToast(data.message, 'error');
                }
            } catch (err) {
                this.showToast('Gagal sinkronisasi metrik Facebook.', 'error');
            } finally {
                this.isSyncing = false;
            }
        },

        async triggerOptimization() {
            this.isOptimizing = true;
            try {
                const res = await fetch(`/api/feedback/optimize${this.pageQuery()}`, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    this.showToast(`Selesai. Topik pemenang: ${data.winning_topic}`);
                    await Promise.all([this.fetchTopics(), this.fetchAnalytics(), this.fetchWindowStats()]);
                } else {
                    this.showToast(data.message || 'Gagal menghitung ulang bobot.', 'error');
                }
            } catch (err) {
                this.showToast('Gagal optimasi feedback loop.', 'error');
            } finally {
                this.isOptimizing = false;
            }
        }
    };
}
