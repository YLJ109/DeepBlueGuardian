/* 深蓝智护 · 移动巡护端
   定位：现场执行设备。桌面端负责调度（派单、看板、报告），移动端负责到场、
   拍照识别、定位存证与结果提交；识别结果一律来自推理节点真实前向输出。 */
(function () {
  'use strict';

  const { api, store, fmt, icon, toast, metricStatus, METRIC_ORDER, METRIC_META, createRealtime } = window.DeepBlue;
  const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;

  const ROLE_LABELS = { admin: '管理员', technician: '技术员', farmer: '养殖户', viewer: '访客' };
  const ROLE_WEIGHT = { viewer: 1, farmer: 1, technician: 2, admin: 3 };
  const PRIORITY_COLOR = { normal: '#33e1ff', high: '#ffd166', urgent: '#ff4d6d' };
  const LEVEL_TAG = { info: 'info', warning: 'warn', critical: 'crit' };

  const TABS = [
    { key: 'home', label: '首页', icon: 'layout' },
    { key: 'patrol', label: '巡护', icon: 'list' },
    { key: 'detect', label: '识别', icon: 'camera' },
    { key: 'alerts', label: '告警', icon: 'bell' },
    { key: 'profile', label: '我的', icon: 'user' },
  ];

  /* ------------------------------------------------------------------ */
  /* 图片压缩与指纹：本机处理，上传前完成，避免大图拖慢移动网络          */
  /* ------------------------------------------------------------------ */
  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片读取失败')); };
      img.src = url;
    });
  }

  async function encodeImage(file, maxSide = 1280, quality = 0.82) {
    const img = await loadImage(file);
    const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, w, h);
    const dataUrl = canvas.toDataURL('image/jpeg', quality);
    const base64 = dataUrl.split(',', 2)[1];
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    let sha256 = '';
    if (window.crypto && window.crypto.subtle) {
      const digest = await window.crypto.subtle.digest('SHA-256', bytes);
      sha256 = Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
    }
    return { dataUrl, base64, sha256, width: w, height: h };
  }

  createApp({
    setup() {
      const logged = ref(!!store.token);
      const user = ref(store.user);
      const loginForm = reactive({ username: 'admin', password: 'deepblue@2026', busy: false, error: '' });
      const tab = ref('home');
      const netStatus = ref('connecting');
      const mainRef = ref(null);

      /* 首页 */
      const regions = ref([]);
      const activeRegion = ref('');
      const overview = ref(null);
      const averages = ref(null);
      const latestReading = ref(null);
      const riskRegions = ref([]);
      const patrolSum = ref(null);

      /* 巡护 */
      const tasks = ref([]);
      const patrolBusy = ref(false);
      const patrolFilter = ref('');
      const actingCode = ref('');
      const dispatchOpen = ref(false);
      const dispatchBusy = ref(false);
      const dispatchForm = reactive({ title: '', region_code: '', priority: 'normal', detail: '' });
      const submitTask = ref(null);
      const submitBusy = ref(false);
      const evidenceBusy = ref(false);
      const evidence = ref(null);
      const evidenceError = ref('');
      const evidenceRef = ref(null);
      const geoBusy = ref(false);
      const geoNote = ref('未定位。可点击定位获取经纬度，定位不可用时如实记录为未获取。');
      const submitForm = reactive({ result_note: '', location_note: '', latitude: null, longitude: null, create_alert: false });
      const detailTask = ref(null);

      /* 识别 */
      const devices = ref([]);
      const samples = ref([]);
      const activeSample = ref('');
      const detectDevice = ref('');
      const detectBusy = ref(false);
      const detectError = ref('');
      const detectResult = ref(null);
      const detectImage = ref('');
      const toAlertBusy = ref(false);
      const fileRef = ref(null);

      /* 告警 */
      const alerts = ref([]);
      const alertStats = ref(null);
      const alertTab = ref('pending');
      const alertBusy = ref(false);
      const activeAlert = ref(null);
      const handleNote = ref('');
      const handleBusy = ref(false);

      /* 我的 */
      const askText = ref('');
      const asking = ref(false);
      const answer = ref('');
      const answerTools = ref([]);
      const speakEnabled = ref(false);
      const speaking = ref(false);
      const listening = ref(false);
      const agentStatus = ref(null);
      const visionStatus = ref(null);
      const speechSupported = typeof window !== 'undefined' && ('speechSynthesis' in window);
      const listenSupported = typeof window !== 'undefined' && !!(window.SpeechRecognition || window.webkitSpeechRecognition);

      const isTechnician = computed(() => !!user.value && (ROLE_WEIGHT[user.value.role] || 0) >= 2);
      const netClass = computed(() => (netStatus.value === 'online' ? 'online' : netStatus.value === 'offline' ? 'offline' : ''));
      const netText = computed(() => (netStatus.value === 'online' ? '实时' : netStatus.value === 'offline' ? '离线' : '连接中'));
      const regionLabel = computed(() => {
        const hit = regions.value.find((r) => r.code === activeRegion.value);
        return hit ? hit.name : '未选择片区';
      });

      const latestMetrics = computed(() => {
        const values = (latestReading.value && latestReading.value.values) || {};
        return METRIC_ORDER.map((key) => {
          const meta = METRIC_META[key];
          const raw = values[key];
          return {
            key,
            label: meta.label,
            unit: meta.unit,
            value: raw === null || raw === undefined ? '--' : Number(raw).toFixed(meta.decimals),
            state: metricStatus(key, raw),
          };
        });
      });

      const patrolFilters = [
        { key: '', label: '全部' },
        { key: 'pending', label: '待领取' },
        { key: 'working', label: '进行中' },
        { key: 'done', label: '已完成' },
      ];

      const filteredTasks = computed(() => {
        if (!patrolFilter.value) return tasks.value;
        return tasks.value.filter((t) => t.status === patrolFilter.value);
      });

      function roleLabel(role) { return ROLE_LABELS[role] || role || '访客'; }
      function priorityColor(p) { return PRIORITY_COLOR[p] || '#33e1ff'; }
      function priorityTag(p) { return p === 'urgent' ? 'crit' : p === 'high' ? 'warn' : 'info'; }
      function levelTag(l) { return LEVEL_TAG[l] || 'mute'; }

      /* ---------------------------------------------------------------- */
      /* 登录与初始化                                                     */
      /* ---------------------------------------------------------------- */
      async function doLogin() {
        loginForm.busy = true;
        loginForm.error = '';
        try {
          const res = await api.login(loginForm.username.trim(), loginForm.password);
          const token = res.access_token || (res.data && res.data.access_token);
          const profile = res.user || (res.data && res.data.user) || null;
          if (!token) throw new Error('登录返回缺少令牌');
          store.token = token;
          if (profile) store.user = profile;
          user.value = profile || store.user;
          if (!user.value) {
            try { const me = await api.me(); user.value = me.user || me.data || me; store.user = user.value; } catch (e) { /* 保持空 */ }
          }
          logged.value = true;
          await bootstrap();
        } catch (err) {
          loginForm.error = err && err.message ? err.message : '登录失败';
        } finally {
          loginForm.busy = false;
        }
      }

      function logout() {
        stopSpeak();
        store.clear();
        user.value = null;
        logged.value = false;
      }

      async function bootstrap() {
        await Promise.allSettled([loadRegions(), loadHome(), loadDevices(), loadSamples(), loadPatrol(), loadAlerts(), loadProfile()]);
      }

      /* ---------------------------------------------------------------- */
      /* 首页                                                             */
      /* ---------------------------------------------------------------- */
      async function loadRegions() {
        try {
          const res = await api.regions();
          regions.value = (res && res.data) || [];
          if (!activeRegion.value && regions.value.length) {
            activeRegion.value = regions.value[0].code;
            await loadRegionLatest();
          }
        } catch (e) { /* 区域不可用时首页其余卡片仍可展示 */ }
      }

      async function loadHome() {
        try {
          const res = await api.overview();
          overview.value = (res && res.summary) || null;
          averages.value = (res && res.averages) || null;
          if (!latestReading.value && res && Array.isArray(res.latest) && res.latest.length) {
            latestReading.value = res.latest[0];
          }
        } catch (e) { /* 保留旧值 */ }
        try {
          const p = await api.predictOverview(48);
          const regionsRank = ((p && p.summary && p.summary.regions) || []).slice()
            .sort((a, b) => (b.max_probability || 0) - (a.max_probability || 0));
          riskRegions.value = regionsRank.slice(0, 4);
        } catch (e) { /* 保留旧值 */ }
        try {
          const s = await api.patrolSummary();
          patrolSum.value = (s && s.data) || null;
        } catch (e) { /* 保留旧值 */ }
      }

      async function loadRegionLatest() {
        if (!activeRegion.value) return;
        try {
          const res = await api.latest(activeRegion.value);
          const list = (res && res.data) || [];
          latestReading.value = list.find((d) => d.status === 'online') || list[0] || null;
        } catch (e) { /* 保留旧值 */ }
      }

      function selectRegion(code) {
        activeRegion.value = code;
        loadRegionLatest();
      }

      /* ---------------------------------------------------------------- */
      /* 巡护任务                                                         */
      /* ---------------------------------------------------------------- */
      async function loadPatrol(manual = false) {
        patrolBusy.value = true;
        try {
          const [list, sum] = await Promise.all([api.patrolTasks({ limit: 200 }), api.patrolSummary()]);
          tasks.value = (list && list.data) || [];
          patrolSum.value = (sum && sum.data) || patrolSum.value;
        } catch (err) {
          if (manual) toast(err.message || '任务加载失败', 'crit');
        } finally {
          patrolBusy.value = false;
        }
      }

      async function createTask() {
        if (!dispatchForm.title.trim()) { toast('请填写任务标题', 'warn'); return; }
        dispatchBusy.value = true;
        try {
          await api.patrolCreate({
            title: dispatchForm.title.trim(),
            detail: dispatchForm.detail.trim() || null,
            region_code: dispatchForm.region_code || null,
            priority: dispatchForm.priority,
            due_hours: dispatchForm.priority === 'urgent' ? 4 : dispatchForm.priority === 'high' ? 12 : 24,
          });
          toast('任务已派发', 'ok');
          dispatchForm.title = '';
          dispatchForm.detail = '';
          dispatchForm.region_code = '';
          dispatchForm.priority = 'normal';
          dispatchOpen.value = false;
          await loadPatrol();
        } catch (err) {
          toast(err.message || '派发失败', 'crit');
        } finally {
          dispatchBusy.value = false;
        }
      }

      async function claimTask(task) {
        actingCode.value = task.task_code;
        try {
          await api.patrolClaim(task.task_code, { assignee: user.value ? user.value.username : null });
          toast('已领取任务', 'ok');
          await loadPatrol();
        } catch (err) {
          toast(err.message || '领取失败', 'crit');
        } finally {
          actingCode.value = '';
        }
      }

      async function openTaskDetail(task) {
        try {
          const res = await api.patrolTask(task.task_code);
          detailTask.value = (res && res.data) || task;
        } catch (e) {
          detailTask.value = task;
        }
      }

      function openSubmit(task) {
        detailTask.value = null;
        submitTask.value = task;
        submitForm.result_note = '';
        submitForm.location_note = task.location_note || '';
        submitForm.latitude = null;
        submitForm.longitude = null;
        submitForm.create_alert = false;
        evidence.value = null;
        evidenceError.value = '';
        geoNote.value = '未定位。可点击定位获取经纬度，定位不可用时如实记录为未获取。';
      }

      function closeSubmit() {
        submitTask.value = null;
        evidence.value = null;
        evidenceError.value = '';
      }

      function pickEvidence() { if (evidenceRef.value) evidenceRef.value.click(); }

      async function onPickEvidence(evt) {
        const file = evt.target.files && evt.target.files[0];
        evt.target.value = '';
        if (!file) return;
        evidenceBusy.value = true;
        evidenceError.value = '';
        try {
          const encoded = await encodeImage(file);
          const res = await api.visionDetect({
            image_base64: encoded.base64,
            device_code: (submitTask.value && submitTask.value.device_code) || undefined,
          });
          const data = res.data || {};
          evidence.value = {
            preview: (data.annotated_jpeg_base64 ? 'data:image/jpeg;base64,' + data.annotated_jpeg_base64 : encoded.dataUrl),
            raw: encoded.dataUrl,
            base64: encoded.base64,
            sha256: encoded.sha256 || '',
            image_name: file.name || 'patrol.jpg',
            model: (data.inference && data.inference.model) || '',
            detections: data.detections || [],
            summary: data.focus_summary || (data.total ? '识别到 ' + data.total + ' 个目标' : '未识别到关注目标'),
            captured_at: data.captured_at || new Date().toISOString().slice(0, 19),
          };
          if (!data.detections || !data.detections.length) {
            evidenceError.value = '本次未识别到关注目标，提交时按空结果如实记录。';
          }
        } catch (err) {
          evidenceError.value = '识别失败：' + (err.message || '推理节点不可用');
        } finally {
          evidenceBusy.value = false;
        }
      }

      function locate() {
        if (!navigator.geolocation) {
          geoNote.value = '当前环境不支持定位，已如实记录为未获取位置。';
          toast('当前环境不支持定位', 'warn');
          return;
        }
        geoBusy.value = true;
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            submitForm.latitude = Number(pos.coords.latitude.toFixed(6));
            submitForm.longitude = Number(pos.coords.longitude.toFixed(6));
            geoNote.value = '定位成功，精度约 ' + Math.round(pos.coords.accuracy) + ' 米，将随存证指纹一并提交。';
            geoBusy.value = false;
          },
          () => {
            geoNote.value = '定位未授权或不可用，已如实记录为未获取位置，可在位置说明中手动描述。';
            geoBusy.value = false;
          },
          { enableHighAccuracy: true, timeout: 8000, maximumAge: 30000 },
        );
      }

      async function submitPatrol() {
        if (!submitTask.value) return;
        submitBusy.value = true;
        try {
          const ev = evidence.value;
          const payload = {
            result_note: submitForm.result_note.trim() || null,
            latitude: submitForm.latitude,
            longitude: submitForm.longitude,
            location_note: submitForm.location_note.trim() || null,
            evidence: ev
              ? {
                  image_sha256: ev.sha256 || null,
                  image_name: ev.image_name,
                  detections: ev.detections.map((d) => ({
                    label: d.label || d.name,
                    name: d.name,
                    category: d.category,
                    level: d.level,
                    confidence: d.confidence,
                    focus: !!d.focus,
                    bbox: d.bbox,
                  })),
                  model: ev.model || null,
                  captured_at: ev.captured_at,
                }
              : null,
            create_alert: !!submitForm.create_alert,
          };
          const res = await api.patrolSubmit(submitTask.value.task_code, payload);
          const data = (res && res.data) || {};
          toast('现场记录已提交并完成存证', 'ok');
          if (data.alert) toast('已同步生成平台告警', 'ok');
          closeSubmit();
          await loadPatrol();
          await loadHome();
        } catch (err) {
          toast(err.message || '提交失败', 'crit');
        } finally {
          submitBusy.value = false;
        }
      }

      const locationText = computed(() => {
        if (submitForm.latitude === null || submitForm.longitude === null) return '';
        return submitForm.latitude + ', ' + submitForm.longitude;
      });

      /* ---------------------------------------------------------------- */
      /* 拍照识别                                                         */
      /* ---------------------------------------------------------------- */
      async function loadDevices() {
        try {
          const res = await api.devices({ limit: 200 });
          devices.value = (res && res.data) || [];
        } catch (e) { /* 忽略 */ }
      }

      async function loadSamples() {
        try {
          const res = await api.visionSamples();
          samples.value = (res && res.data) || [];
        } catch (e) { /* 忽略 */ }
      }

      function pickFile() { if (fileRef.value) fileRef.value.click(); }

      function applyDetectResult(data, fallbackPreview) {
        detectResult.value = data;
        if (data.annotated_jpeg_base64) detectImage.value = 'data:image/jpeg;base64,' + data.annotated_jpeg_base64;
        else if (fallbackPreview) detectImage.value = fallbackPreview;
        else detectImage.value = '';
      }

      async function onPickFile(evt) {
        const file = evt.target.files && evt.target.files[0];
        evt.target.value = '';
        if (!file) return;
        detectBusy.value = true;
        detectError.value = '';
        activeSample.value = '';
        try {
          const encoded = await encodeImage(file);
          const res = await api.visionDetect({ image_base64: encoded.base64, device_code: detectDevice.value || undefined });
          applyDetectResult(res.data || {}, encoded.dataUrl);
        } catch (err) {
          detectError.value = '识别失败：' + (err.message || '推理节点不可用');
          detectResult.value = null;
        } finally {
          detectBusy.value = false;
        }
      }

      async function detectSample(name) {
        detectBusy.value = true;
        detectError.value = '';
        activeSample.value = name;
        try {
          const res = await api.visionDetect({ sample: name, device_code: detectDevice.value || undefined });
          applyDetectResult(res.data || {}, '');
        } catch (err) {
          detectError.value = '识别失败：' + (err.message || '推理节点不可用');
          detectResult.value = null;
        } finally {
          detectBusy.value = false;
        }
      }

      async function detectToAlert() {
        const data = detectResult.value;
        const focus = data && data.focus_hits && data.focus_hits[0];
        if (!focus) { toast('本次没有可转告警的目标', 'warn'); return; }
        if (!detectDevice.value) { toast('请先选择关联监测点位', 'warn'); return; }
        toAlertBusy.value = true;
        try {
          await api.visionAlert({
            device_code: detectDevice.value,
            label: focus.label || focus.name,
            confidence: focus.confidence,
            note: '移动端现场拍照识别上报：' + (data.focus_summary || ''),
            sample: data.sample ? data.sample.name : null,
          });
          toast('已生成平台告警', 'ok');
          await loadHome();
          await loadAlerts();
        } catch (err) {
          toast(err.message || '转告警失败', 'crit');
        } finally {
          toAlertBusy.value = false;
        }
      }

      /* ---------------------------------------------------------------- */
      /* 告警                                                             */
      /* ---------------------------------------------------------------- */
      async function loadAlerts() {
        alertBusy.value = true;
        try {
          const [list, stats] = await Promise.all([
            api.alerts({ status: alertTab.value, hours: 72, limit: 100 }),
            api.alertStats(72),
          ]);
          alerts.value = (list && list.data) || [];
          alertStats.value = (stats && stats.data) || null;
        } catch (err) {
          toast(err.message || '告警加载失败', 'crit');
        } finally {
          alertBusy.value = false;
        }
      }

      function switchAlertTab(key) {
        if (alertTab.value === key) return;
        alertTab.value = key;
        alerts.value = [];
        loadAlerts();
      }

      function openAlert(alert) {
        activeAlert.value = alert;
        handleNote.value = alert.handle_note || '';
      }

      function closeAlert() { activeAlert.value = null; }

      async function resolveAlert() {
        if (!activeAlert.value) return;
        handleBusy.value = true;
        try {
          await api.handleAlert(activeAlert.value.id, { status: 'resolved', note: handleNote.value.trim() || null });
          toast('告警已处置', 'ok');
          closeAlert();
          await loadAlerts();
          await loadHome();
        } catch (err) {
          toast(err.message || '处置失败', 'crit');
        } finally {
          handleBusy.value = false;
        }
      }

      async function alertToTask() {
        const alert = activeAlert.value;
        if (!alert) return;
        handleBusy.value = true;
        try {
          await api.patrolCreate({
            title: '现场核查：' + alert.title,
            detail: alert.content,
            region_code: alert.region_code || null,
            device_code: alert.device_code || null,
            priority: alert.level === 'critical' ? 'urgent' : 'high',
            due_hours: alert.level === 'critical' ? 4 : 12,
            source_alert: alert.alert_code,
          });
          toast('已转为巡护任务', 'ok');
          closeAlert();
          await loadPatrol();
          switchTab('patrol');
        } catch (err) {
          toast(err.message || '转派失败', 'crit');
        } finally {
          handleBusy.value = false;
        }
      }

      /* ---------------------------------------------------------------- */
      /* 我的：问数与播报                                                 */
      /* ---------------------------------------------------------------- */
      async function loadProfile() {
        try { const res = await api.agentStatus(); agentStatus.value = (res && res.data) || null; } catch (e) { /* 忽略 */ }
        try { const res = await api.visionStatus(); visionStatus.value = (res && res.data) || null; } catch (e) { /* 忽略 */ }
      }

      async function askAgent() {
        const question = askText.value.trim();
        if (!question || asking.value) return;
        stopSpeak();
        asking.value = true;
        answer.value = '';
        answerTools.value = [];
        try {
          const res = await api.agentChat(question, []);
          const data = (res && res.data) || {};
          answer.value = data.answer || '暂未获得回答';
          answerTools.value = data.tools || [];
          if (speakEnabled.value) speak(answer.value);
        } catch (err) {
          answer.value = '提问失败：' + (err.message || '问数服务不可用');
        } finally {
          asking.value = false;
        }
      }

      /* 语音播报：始终单路，先取消再播，避免重叠与重复 */
      let utter = null;
      let speakToken = 0;
      function speak(text) {
        if (!speechSupported || !text) return;
        const token = ++speakToken;
        window.speechSynthesis.cancel();
        setTimeout(() => {
          if (token !== speakToken) return;
          utter = new SpeechSynthesisUtterance(text);
          utter.lang = 'zh-CN';
          utter.rate = 1.02;
          utter.onend = () => { if (token === speakToken) { speaking.value = false; utter = null; } };
          utter.onerror = () => { if (token === speakToken) { speaking.value = false; utter = null; } };
          speaking.value = true;
          window.speechSynthesis.speak(utter);
        }, 140);
      }

      function stopSpeak() {
        speakToken += 1;
        if (speechSupported) window.speechSynthesis.cancel();
        utter = null;
        speaking.value = false;
      }

      function onSpeakToggle() {
        if (speakEnabled.value && answer.value) speak(answer.value);
        else stopSpeak();
      }

      let recognition = null;
      function toggleListen() {
        if (!listenSupported) { toast('当前浏览器不支持语音识别', 'warn'); return; }
        if (listening.value) {
          if (recognition) recognition.stop();
          return;
        }
        stopSpeak();
        const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
        recognition = new Ctor();
        recognition.lang = 'zh-CN';
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.onstart = () => { listening.value = true; };
        recognition.onerror = () => { listening.value = false; toast('没有听清，请重试或直接输入', 'warn'); };
        recognition.onend = () => { listening.value = false; };
        recognition.onresult = (event) => {
          const text = event.results[0][0].transcript || '';
          askText.value = text;
          listening.value = false;
          if (text) askAgent();
        };
        recognition.start();
      }

      /* ---------------------------------------------------------------- */
      /* 实时通道与自动刷新                                               */
      /* ---------------------------------------------------------------- */
      let realtime = null;
      let lastRefresh = 0;
      let refreshing = false;

      function connectRealtime() {
        if (realtime) realtime.close();
        realtime = createRealtime({
          onStatus: (s) => { netStatus.value = s; },
          onEvent: (payload) => {
            const type = payload && payload.type;
            if (type !== 'reading' && type !== 'alert' && type !== 'device' && type !== 'aggregate') return;
            const now = Date.now();
            if (refreshing || now - lastRefresh < 15000) return;
            lastRefresh = now;
            refreshing = true;
            const job = tab.value === 'alerts' ? loadAlerts() : tab.value === 'home' ? loadHome() : loadPatrol();
            Promise.resolve(job).finally(() => { refreshing = false; });
          },
        });
      }

      function switchTab(key) {
        tab.value = key;
        if (mainRef.value) mainRef.value.scrollTop = 0;
        if (key === 'home') loadHome();
        else if (key === 'patrol') loadPatrol();
        else if (key === 'alerts') loadAlerts();
        else if (key === 'profile') loadProfile();
        else if (key === 'detect' && !samples.value.length) loadSamples();
      }

      onMounted(() => {
        if (logged.value) {
          bootstrap();
        }
        connectRealtime();
        setInterval(() => {
          if (!logged.value || document.hidden) return;
          if (tab.value === 'home') loadHome();
          else if (tab.value === 'patrol') loadPatrol();
        }, 45000);
      });

      return {
        logged, user, loginForm, doLogin, logout,
        tab, TABS, switchTab, netClass, netText, mainRef,
        icon, fmt, roleLabel, isTechnician,
        regions, activeRegion, regionLabel, selectRegion, latestReading, latestMetrics,
        overview, riskRegions, patrolSum,
        tasks, patrolBusy, patrolFilter, patrolFilters, filteredTasks, actingCode,
        dispatchOpen, dispatchBusy, dispatchForm, createTask,
        submitTask, submitForm, submitBusy, closeSubmit, openSubmit, submitPatrol,
        evidence, evidenceBusy, evidenceError, evidenceRef, pickEvidence, onPickEvidence,
        locate, geoBusy, geoNote, locationText,
        detailTask, openTaskDetail, claimTask, priorityColor, priorityTag,
        devices, samples, activeSample, detectDevice, detectBusy, detectError,
        detectResult, detectImage, toAlertBusy, fileRef, pickFile, onPickFile, detectSample, detectToAlert,
        alerts, alertStats, alertTab, alertBusy, activeAlert, handleNote, handleBusy,
        loadAlerts, switchAlertTab, openAlert, closeAlert, resolveAlert, alertToTask, levelTag,
        askText, asking, answer, answerTools, speechSupported, listenSupported, listening,
        speakEnabled, speaking, askAgent, toggleListen, onSpeakToggle, stopSpeak,
        agentStatus, visionStatus,
      };
    },
  }).mount('#mobile');
})();
