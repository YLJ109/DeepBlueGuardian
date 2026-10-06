/* 深蓝智护 · 管理后台
   覆盖设备管理、历史数据、告警处置、模型配置、红树林遥感与账号审计。 */
(function () {
  const { api, store, fmt, icon, toast, metricStatus, METRIC_ORDER, METRIC_META } = window.DeepBlue;
  const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;

  const VIEWS = [
    { key: 'overview', label: '运行概览', icon: 'layout' },
    { key: 'devices', label: '设备管理', icon: 'cpu' },
    { key: 'history', label: '历史数据', icon: 'history' },
    { key: 'alerts', label: '告警记录', icon: 'bell' },
    { key: 'patrol', label: '巡护任务', icon: 'list' },
    { key: 'vision', label: '视频AI识别', icon: 'camera' },
    { key: 'model', label: '赤潮模型', icon: 'gauge' },
    { key: 'mangrove', label: '红树林监测', icon: 'leaf' },
    { key: 'accounts', label: '账号与审计', icon: 'users' },
  ];

  /* 顶部指挥条：标题左侧四项、标题右侧三项 */
  const NAV_LEFT = VIEWS.slice(0, 4);
  const NAV_RIGHT = VIEWS.slice(4);
  const ROLE_LABELS = { admin: '管理员', technician: '技术员', farmer: '养殖户', viewer: '访客' };
  function roleLabel(role) { return ROLE_LABELS[role] || role || '访客'; }

  createApp({
    setup() {
      const logged = ref(!!store.token);
      const user = ref(store.user);
      const loginForm = reactive({ username: 'admin', password: 'deepblue@2026', busy: false, error: '' });
      const view = ref('overview');
      /* 账号与审计视图内的标签页：platform 账号 / 新增账号 / 操作审计日志 */
      const accountTab = ref('users');
      const loading = ref(false);

      const overview = ref(null);
      const deviceStats = ref(null);
      const regions = ref([]);
      const devices = ref([]);
      const deviceTotal = ref(0);
      const deviceQuery = reactive({ keyword: '', type: '', region_code: '', status: '' });
      const selectedDevice = ref(null);
      const deviceDetail = ref(null);

      const metricsMeta = ref([]);
      const historyQuery = reactive({ device_code: '', metric: 'chlorophyll_a', granularity: '1h', hours: 24, device_codes: [] });
      const historyResult = ref(null);
      const historyBusy = ref(false);

      const alerts = ref([]);
      const alertTotal = ref(0);
      const alertQuery = reactive({ status: '', level: '', type: '', hours: 168 });
      const alertStats = ref(null);
      const handlingAlert = ref(null);
      const handleNote = ref('');

      const modelInfo = ref(null);
      const modelConfigs = ref([]);
      const modelRules = ref(null);
      const thresholds = reactive({ t1: 0.25, t2: 0.5, t3: 0.75 });

      const mangroveRegions = ref([]);
      const mangroveStats = ref(null);
      const mangroveTrend = ref(null);
      const mangroveAnalysis = ref(null);
      const mangroveBusy = ref(false);

      const visionStatus = ref(null);
      const visionCameras = ref([]);
      const visionSamples = ref([]);
      const visionForm = reactive({ device_code: '', sample: '', model: '', video_path: '', sample_fps: 1, max_frames: 120 });
      const visionModels = ref([]);
      const visionModelBusy = ref(false);
      const visionConf = ref(0.35);
      const visionResult = ref(null);
      const visionScan = ref(null);
      const visionBusy = ref(false);
      const visionScanBusy = ref(false);
      const selectedSample = computed(() => visionSamples.value.find((s) => s.name === visionForm.sample) || null);
      /* 推理节点回传的标注图为 JPEG Base64，这里拼成可直接渲染的地址 */
      const JPEG_PREFIX = ['data', 'image/jpeg;base64'].join(':') + ',';
      function visionImage(base64) { return base64 ? JPEG_PREFIX + base64 : ''; }

      /* 巡护任务：桌面端负责派单、看板与报告，移动端负责现场执行 */
      const patrolTasks = ref([]);
      const patrolSummary = ref(null);
      const patrolReport = ref(null);
      const patrolBusy = ref(false);
      const patrolDetail = ref(null);
      const patrolQuery = reactive({ status: '', region_code: '', hours: 72 });
      const patrolForm = reactive({ title: '', detail: '', region_code: '', device_code: '', priority: 'normal', due_hours: 24, assignee: '' });


      const users = ref([]);
      const activeUsers = computed(() => users.value.filter((u) => u.active).length);
      const auditLogs = ref([]);
      const newUser = reactive({ username: '', display_name: '', role: 'viewer', password: '', phone: '', region_scope: '' });
      const passwordForm = reactive({ old_password: '', new_password: '' });

      const events = ref([]);
      const volume = ref(null);

      const charts = {};
      const AXIS = {
        axisLine: { lineStyle: { color: 'rgba(51,225,255,0.28)' } },
        axisLabel: { color: '#7fb3cc', fontSize: 10, fontFamily: 'Consolas, monospace' },
        splitLine: { lineStyle: { color: 'rgba(51,225,255,0.08)' } },
      };
      const TOOLTIP = {
        trigger: 'axis',
        backgroundColor: 'rgba(6,28,50,0.96)',
        borderColor: 'rgba(51,225,255,0.4)',
        textStyle: { color: '#dff3ff', fontSize: 11 },
      };

      function mountChart(id, option) {
        const el = document.getElementById(id);
        if (!el) return;
        if (charts[id]) charts[id].dispose();
        charts[id] = window.echarts.init(el, null, { renderer: 'canvas' });
        charts[id].setOption(option);
      }

      const summary = computed(() => (overview.value && overview.value.summary) || {});

      async function doLogin() {
        loginForm.busy = true;
        loginForm.error = '';
        try {
          const res = await api.login(loginForm.username.trim(), loginForm.password);
          store.token = res.access_token;
          store.user = res.user;
          user.value = res.user;
          logged.value = true;
          toast(`欢迎，${res.user.display_name}`, 'ok');
          await boot();
        } catch (err) {
          loginForm.error = err.message;
        } finally {
          loginForm.busy = false;
        }
      }

      function logout() {
        store.clear();
        logged.value = false;
        user.value = null;
        toast('已退出登录', 'ok');
      }

      /* 点击标题进入可视化大屏 */
      function openScreen() {
        window.open('/', '_blank', 'noopener');
      }

      async function boot() {
        loading.value = true;
        try {
          await Promise.all([loadOverview(), loadDeviceStats(), loadRegions(), loadEvents(), loadVolume(), loadMetrics()]);
          await loadDevices();
          if (!historyQuery.device_code && devices.value.length) {
            const buoy = devices.value.find((d) => d.type === 'buoy') || devices.value[0];
            historyQuery.device_code = buoy.device_code;
          }
        } catch (err) {
          toast(`数据加载失败：${err.message}`, 'err');
        } finally {
          loading.value = false;
        }
      }

      async function loadOverview() {
        overview.value = await api.overview();
        await nextTick();
        const trend = overview.value.trend || { labels: [], series: {} };
        mountChart('overviewTrend', {
          grid: { left: 42, right: 16, top: 30, bottom: 26 },
          tooltip: TOOLTIP,
          legend: { textStyle: { color: '#7fb3cc', fontSize: 11 }, top: 0, icon: 'roundRect', itemWidth: 11, itemHeight: 8 },
          xAxis: { type: 'category', boundaryGap: false, data: trend.labels, ...AXIS },
          yAxis: { type: 'value', scale: true, ...AXIS },
          series: ['chlorophyll_a', 'dissolved_oxygen', 'water_temp'].map((k) => ({
            name: METRIC_META[k].label,
            type: 'line',
            smooth: true,
            showSymbol: false,
            lineStyle: { width: 2, color: METRIC_META[k].color },
            itemStyle: { color: METRIC_META[k].color },
            areaStyle: k === 'chlorophyll_a' ? { opacity: 0.14, color: METRIC_META[k].color } : undefined,
            data: trend.series[k] || [],
          })),
        });
      }

      async function loadDeviceStats() { deviceStats.value = (await api.deviceStats()).data; }
      async function loadRegions() { regions.value = (await api.regions()).data; }
      async function loadEvents() { events.value = (await api.dashboardEvents(24)).data; }
      async function loadVolume() { volume.value = (await api.volume()).data; }
      async function loadMetrics() { metricsMeta.value = (await api.metrics()).data; }

      async function loadDevices() {
        const res = await api.devices({ ...deviceQuery, limit: 300 });
        devices.value = res.data;
        deviceTotal.value = res.total;
      }

      async function openDevice(code) {
        try {
          const res = await api.device(code);
          deviceDetail.value = res.data;
          selectedDevice.value = code;
        } catch (err) {
          toast(`设备详情加载失败：${err.message}`, 'err');
        }
      }

      async function runDeviceCommand(device, command, payload) {
        try {
          const res = await api.deviceCommand(device.device_code, command, payload);
          toast(`${device.name}：${res.message}（${JSON.stringify(res.applied)}）`, 'ok');
          await loadDevices();
        } catch (err) {
          toast(`指令下发失败：${err.message}`, 'err');
        }
      }

      function changeInterval(device) {
        const next = window.prompt('设置采样间隔（秒，30—3600）', String(device.sample_interval));
        if (next === null) return;
        const value = Number(next);
        if (!Number.isFinite(value) || value < 30 || value > 3600) { toast('采样间隔需在 30—3600 秒之间', 'err'); return; }
        runDeviceCommand(device, 'set_interval', { sample_interval: value });
      }

      async function loadHistory() {
        if (!historyQuery.device_code) { toast('请先选择设备', 'err'); return; }
        historyBusy.value = true;
        try {
          const end = new Date();
          const start = new Date(end.getTime() - historyQuery.hours * 3600 * 1000);
          const series = historyQuery.device_codes.length > 1
            ? await api.compare({
                device_codes: historyQuery.device_codes.join(','),
                metric: historyQuery.metric,
                hours: historyQuery.hours,
                granularity: historyQuery.granularity,
              })
            : await api.history({
                device_code: historyQuery.device_code,
                start: start.toISOString(),
                end: end.toISOString(),
                metrics: historyQuery.metric,
                limit: 8000,
              });
          historyResult.value = series;
          await nextTick();
          renderHistoryChart();
        } catch (err) {
          toast(`历史数据查询失败：${err.message}`, 'err');
        } finally {
          historyBusy.value = false;
        }
      }

      function renderHistoryChart() {
        const data = historyResult.value;
        if (!data) return;
        if (data.series && Array.isArray(data.series) && data.series.length && data.series[0].points) {
          const labels = data.series[0].points.map((p) => p[0].slice(5, 16).replace('T', ' '));
          mountChart('historyChart', {
            grid: { left: 54, right: 18, top: 34, bottom: 28 },
            tooltip: TOOLTIP,
            legend: { textStyle: { color: '#7fb3cc', fontSize: 11 }, top: 0 },
            xAxis: { type: 'category', data: labels, ...AXIS },
            yAxis: { type: 'value', scale: true, name: data.unit, nameTextStyle: { color: '#7fb3cc' }, ...AXIS },
            series: data.series.map((s, i) => ({
              name: s.device_name,
              type: 'line',
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 2, color: ['#33e1ff', '#29e7a0', '#ffd166', '#ff9f43', '#ff4d6d', '#a78bfa'][i % 6] },
              data: s.points.map((p) => p[1]),
            })),
          });
          return;
        }
        const labels = (data.timestamps || []).map((t) => t.slice(5, 16).replace('T', ' '));
        mountChart('historyChart', {
          grid: { left: 54, right: 18, top: 34, bottom: 28 },
          tooltip: TOOLTIP,
          legend: { textStyle: { color: '#7fb3cc', fontSize: 11 }, top: 0 },
          dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 4, borderColor: 'rgba(51,225,255,0.2)', textStyle: { color: '#7fb3cc' } }],
          xAxis: { type: 'category', data: labels, ...AXIS },
          yAxis: { type: 'value', scale: true, name: data.metrics?.[0]?.unit || '', nameTextStyle: { color: '#7fb3cc' }, ...AXIS },
          series: [{
            name: data.metrics?.[0]?.label || historyQuery.metric,
            type: 'line',
            smooth: true,
            showSymbol: false,
            lineStyle: { width: 2, color: '#33e1ff' },
            areaStyle: { opacity: 0.12, color: '#33e1ff' },
            data: data.series[historyQuery.metric] || [],
          }],
        });
      }

      function exportCsv() {
        if (!historyQuery.device_code) { toast('请先选择设备', 'err'); return; }
        const end = new Date();
        const start = new Date(end.getTime() - historyQuery.hours * 3600 * 1000);
        const url = api.exportUrl({
          device_code: historyQuery.device_code,
          start: start.toISOString(),
          end: end.toISOString(),
        });
        window.open(url, '_blank');
      }

      async function loadAlerts() {
        const [list, stats] = await Promise.all([api.alerts({ ...alertQuery, limit: 200 }), api.alertStats(alertQuery.hours)]);
        alerts.value = list.data;
        alertTotal.value = list.total;
        alertStats.value = stats.data;
        await nextTick();
        const byType = stats.data.by_type || {};
        mountChart('alertTypeChart', {
          grid: { left: 60, right: 20, top: 20, bottom: 24 },
          tooltip: { ...TOOLTIP, trigger: 'axis', axisPointer: { type: 'shadow' } },
          xAxis: { type: 'value', ...AXIS },
          yAxis: { type: 'category', data: Object.keys(byType).map((k) => ({ threshold: '阈值越限', trend: '趋势异常', model: '模型预警', device: '设备异常' }[k] || k)), ...AXIS },
          series: [{
            type: 'bar',
            barWidth: 12,
            data: Object.values(byType).map((v) => ({ value: v, itemStyle: { color: '#33e1ff', borderRadius: [0, 3, 3, 0] } })),
            label: { show: true, position: 'right', color: '#dff3ff', fontSize: 11, fontFamily: 'Consolas, monospace' },
          }],
        });
      }

      async function scanAlerts() {
        try {
          const res = await api.alertScan();
          toast(`巡检完成，新增离线告警 ${res.offline_alerts} 条`, 'ok');
          await loadAlerts();
        } catch (err) { toast(`巡检失败：${err.message}`, 'err'); }
      }

      function openHandle(alert) { handlingAlert.value = alert; handleNote.value = ''; }

      async function submitHandle(status) {
        if (!handlingAlert.value) return;
        try {
          await api.handleAlert(handlingAlert.value.id, { status, note: handleNote.value || null });
          toast('处置状态已更新', 'ok');
          handlingAlert.value = null;
          await loadAlerts();
        } catch (err) { toast(`提交失败：${err.message}`, 'err'); }
      }

      /* 巡护任务：与 /api/v1/patrol 对接，桌面端只做调度与查看 */
      async function loadPatrol() {
        patrolBusy.value = true;
        try {
          const [list, sum, rep] = await Promise.all([
            api.patrolTasks({ ...patrolQuery, limit: 200 }),
            api.patrolSummary(),
            api.patrolReport(patrolQuery.hours),
          ]);
          patrolTasks.value = list.data || [];
          patrolSummary.value = sum.data || null;
          patrolReport.value = rep.data || null;
        } catch (err) {
          toast(`巡护任务加载失败：${err.message}`, 'err');
        } finally {
          patrolBusy.value = false;
        }
      }

      async function createPatrol() {
        if (!patrolForm.title.trim()) { toast('请填写任务标题', 'warn'); return; }
        try {
          await api.patrolCreate({
            title: patrolForm.title.trim(),
            detail: patrolForm.detail.trim() || null,
            region_code: patrolForm.region_code || null,
            device_code: patrolForm.device_code || null,
            priority: patrolForm.priority,
            due_hours: Number(patrolForm.due_hours) || 24,
            assignee: patrolForm.assignee.trim() || null,
          });
          toast('巡护任务已派发，移动端可领取执行', 'ok');
          patrolForm.title = '';
          patrolForm.detail = '';
          patrolForm.assignee = '';
          await loadPatrol();
        } catch (err) { toast(`派发失败：${err.message}`, 'err'); }
      }

      async function openPatrol(code) {
        try {
          patrolDetail.value = (await api.patrolTask(code)).data;
        } catch (err) { toast(`任务详情加载失败：${err.message}`, 'err'); }
      }

      function patrolPriorityTag(p) { return p === 'urgent' ? 'crit' : p === 'high' ? 'warn' : 'info'; }
      function patrolStatusTag(s) { return s === 'done' ? 'ok' : s === 'working' ? 'info' : 'warn'; }


      async function loadModel() {
        const [info, configs, rules] = await Promise.all([api.modelInfo(), api.modelConfigs(), api.modelRules()]);
        modelInfo.value = info.data;
        modelConfigs.value = configs.data;
        modelRules.value = rules.data;
        if (info.data.thresholds && info.data.thresholds.length === 3) {
          thresholds.t1 = info.data.thresholds[0];
          thresholds.t2 = info.data.thresholds[1];
          thresholds.t3 = info.data.thresholds[2];
        }
        await nextTick();
        const perClass = info.data.per_class || [];
        if (perClass.length) {
          mountChart('modelChart', {
            grid: { left: 50, right: 20, top: 30, bottom: 26 },
            tooltip: { ...TOOLTIP, trigger: 'axis', axisPointer: { type: 'shadow' } },
            legend: { textStyle: { color: '#7fb3cc', fontSize: 11 }, top: 0 },
            xAxis: { type: 'category', data: perClass.map((c) => c.label), ...AXIS },
            yAxis: { type: 'value', min: 0, max: 1, ...AXIS },
            series: [
              { name: '精确率', type: 'bar', barWidth: 14, itemStyle: { color: '#33e1ff', borderRadius: [3, 3, 0, 0] }, data: perClass.map((c) => c.precision) },
              { name: '召回率', type: 'bar', barWidth: 14, itemStyle: { color: '#29e7a0', borderRadius: [3, 3, 0, 0] }, data: perClass.map((c) => c.recall) },
              { name: 'F1', type: 'bar', barWidth: 14, itemStyle: { color: '#ffd166', borderRadius: [3, 3, 0, 0] }, data: perClass.map((c) => c.f1) },
            ],
          });
        }
        const matrix = info.data.confusion_matrix;
        if (matrix && matrix.matrix) {
          const flat = [];
          matrix.matrix.forEach((row, i) => row.forEach((v, j) => flat.push([j, i, v])));
          mountChart('confusionChart', {
            grid: { left: 56, right: 30, top: 26, bottom: 34 },
            tooltip: { position: 'top', backgroundColor: 'rgba(6,28,50,0.96)', borderColor: 'rgba(51,225,255,0.4)', textStyle: { color: '#dff3ff', fontSize: 11 } },
            xAxis: { type: 'category', data: matrix.labels.map((l) => `${l}级`), ...AXIS, name: '预测', nameTextStyle: { color: '#7fb3cc' } },
            yAxis: { type: 'category', data: matrix.labels.map((l) => `${l}级`), ...AXIS, name: '实际', nameTextStyle: { color: '#7fb3cc' } },
            visualMap: { min: 0, max: Math.max(...flat.map((f) => f[2])), calculable: true, orient: 'horizontal', left: 'center', bottom: 0, textStyle: { color: '#7fb3cc', fontSize: 10 }, inRange: { color: ['#062a46', '#33e1ff', '#29e7a0'] } },
            series: [{ type: 'heatmap', data: flat, label: { show: true, color: '#dff3ff', fontSize: 11, fontFamily: 'Consolas, monospace' }, itemStyle: { borderColor: 'rgba(4,16,31,0.9)', borderWidth: 1 } }],
          });
        }
      }

      async function saveThresholds() {
        const target = modelConfigs.value.find((c) => c.name === 'red_tide' && c.active)
          || modelConfigs.value.find((c) => c.name === 'red_tide');
        if (!target) { toast('未找到赤潮模型配置', 'err'); return; }
        const list = [Number(thresholds.t1), Number(thresholds.t2), Number(thresholds.t3)];
        try {
          await api.updateModelConfig(target.id, { params: { thresholds: list } });
          toast('风险分级阈值已保存，后续预测立即生效', 'ok');
          await loadModel();
        } catch (err) { toast(`保存失败：${err.message}`, 'err'); }
      }

      async function reloadModel() {
        try {
          const res = await api.reloadModel('v1');
          toast(res.message, res.loaded ? 'ok' : 'err');
          await loadModel();
        } catch (err) { toast(`重载失败：${err.message}`, 'err'); }
      }

      async function loadMangrove() {
        const [regionsRes, statsRes, trendRes] = await Promise.all([
          api.mangroveRegions(), api.mangroveStats(), api.mangroveTrend(),
        ]);
        mangroveRegions.value = regionsRes.data;
        mangroveStats.value = statsRes.data;
        mangroveTrend.value = trendRes;
        await nextTick();
        mountChart('mangroveChart', {
          grid: { left: 46, right: 18, top: 34, bottom: 26 },
          tooltip: TOOLTIP,
          legend: { textStyle: { color: '#7fb3cc', fontSize: 11 }, top: 0 },
          xAxis: { type: 'category', data: trendRes.periods, ...AXIS },
          yAxis: { type: 'value', min: 50, max: 100, ...AXIS },
          series: trendRes.series.map((s, i) => ({
            name: s.region_name,
            type: 'line',
            smooth: true,
            lineStyle: { width: 2, color: ['#29e7a0', '#33e1ff', '#ffd166'][i % 3] },
            itemStyle: { color: ['#29e7a0', '#33e1ff', '#ffd166'][i % 3] },
            data: s.values,
          })),
        });
      }

      async function runMangroveAnalysis(regionCode) {
        if (!regionCode) return;
        mangroveBusy.value = true;
        try {
          mangroveAnalysis.value = await api.mangroveAnalysis(regionCode, 0.35);
        } catch (err) {
          toast(`遥感分析失败：${err.message}`, 'err');
        } finally {
          mangroveBusy.value = false;
        }
      }

      /* ---------------- 视频 AI 识别 ---------------- */
      async function loadVision() {
        const res = await api.visionStatus();
        visionStatus.value = res.data;
        visionCameras.value = res.data.cameras || [];
        visionSamples.value = res.data.samples || [];
        if (!visionForm.device_code && visionCameras.value.length) visionForm.device_code = visionCameras.value[0].device_code;
        if (!visionForm.sample && visionSamples.value.length) visionForm.sample = visionSamples.value[0].name;
        visionConf.value = res.data.conf_threshold || 0.35;
        visionModels.value = res.data.available_models || [];
        const activeModel = visionModels.value.find((m) => m.active);
        if (activeModel) visionForm.model = activeModel.name;
      }

      async function runVisionDetect(imageBase64) {
        const body = { device_code: visionForm.device_code || null, conf: visionConf.value };
        if (imageBase64) body.image_base64 = imageBase64;
        else body.sample = visionForm.sample;
        if (!body.sample && !body.image_base64) { toast('请先选择样本影像或上传一张图片', 'err'); return; }
        visionBusy.value = true;
        try {
          const res = await api.visionDetect(body);
          visionResult.value = res.data;
          toast(res.data.focus_summary, res.data.has_focus ? 'ok' : 'err');
        } catch (err) {
          toast(`识别失败：${err.message}`, 'err');
        } finally {
          visionBusy.value = false;
        }
      }

      function onVisionUpload(evt) {
        const file = evt.target.files && evt.target.files[0];
        evt.target.value = '';
        if (!file) return;
        if (file.size > 12 * 1024 * 1024) { toast('图片请控制在 12 MB 以内', 'err'); return; }
        const reader = new FileReader();
        reader.onload = () => runVisionDetect(String(reader.result));
        reader.onerror = () => toast('图片读取失败', 'err');
        reader.readAsDataURL(file);
      }

      async function runVisionScan() {
        if (!visionForm.video_path) { toast('请填写本地录像的完整路径', 'err'); return; }
        visionScanBusy.value = true;
        try {
          const res = await api.visionScan({
            device_code: visionForm.device_code || null,
            video_path: visionForm.video_path,
            sample_fps: visionForm.sample_fps,
            max_frames: visionForm.max_frames,
            conf: visionConf.value,
          });
          visionScan.value = res.data;
          toast(`巡视完成：采样 ${res.data.scan.scanned_frames} 帧，识别目标 ${res.data.scan.total_objects} 个`, 'ok');
        } catch (err) {
          toast(`巡视失败：${err.message}`, 'err');
        } finally {
          visionScanBusy.value = false;
        }
      }

      async function switchVisionModel() {
        if (!visionForm.model) return;
        visionModelBusy.value = true;
        try {
          const res = await api.visionSwitchModel(visionForm.model);
          toast(`识别权重已切换为 ${res.model}`, 'ok');
          await loadVision();
        } catch (err) {
          toast(`权重切换失败：${err.message}`, 'err');
        } finally {
          visionModelBusy.value = false;
        }
      }

      async function raiseVisionAlert(det) {
        if (!visionForm.device_code) { toast('请先选择关联点位', 'err'); return; }
        try {
          const res = await api.visionAlert({
            device_code: visionForm.device_code,
            label: det.name,
            confidence: det.confidence,
            sample: (visionResult.value && visionResult.value.sample) ? visionResult.value.sample.name : null,
          });
          toast(res.message, res.created ? 'ok' : 'err');
        } catch (err) {
          toast(`生成告警失败：${err.message}`, 'err');
        }
      }

      async function loadAccounts() {
        const [u, a] = await Promise.all([api.users(), api.audit(120)]);
        users.value = u.data;
        auditLogs.value = a.data;
      }

      async function createUser() {
        if (!newUser.username || newUser.password.length < 8) { toast('用户名必填，密码不少于 8 位', 'err'); return; }
        try {
          await api.createUser({ ...newUser });
          toast('账号已创建', 'ok');
          Object.assign(newUser, { username: '', display_name: '', role: 'viewer', password: '', phone: '', region_scope: '' });
          await loadAccounts();
        } catch (err) { toast(`创建失败：${err.message}`, 'err'); }
      }

      async function toggleUser(u) {
        try {
          await api.updateUser(u.id, { active: !u.active });
          toast(u.active ? '账号已停用' : '账号已启用', 'ok');
          await loadAccounts();
        } catch (err) { toast(`操作失败：${err.message}`, 'err'); }
      }

      async function changePassword() {
        if (passwordForm.new_password.length < 8) { toast('新密码不少于 8 位', 'err'); return; }
        try {
          await api.changePassword(passwordForm.old_password, passwordForm.new_password);
          toast('密码已更新', 'ok');
          passwordForm.old_password = '';
          passwordForm.new_password = '';
        } catch (err) { toast(`修改失败：${err.message}`, 'err'); }
      }

      function resizeCharts() {
        Object.values(charts).forEach((c) => { try { if (c && !c.isDisposed()) c.resize(); } catch (err) { /* 忽略已销毁实例 */ } });
      }

      async function switchView(key) {
        view.value = key;
        try {
          if (key === 'devices') { await loadDevices(); await loadRegions(); }
          if (key === 'history') { if (!metricsMeta.value.length) await loadMetrics(); await loadHistory(); }
          if (key === 'alerts') await loadAlerts();
          if (key === 'patrol') await loadPatrol();
          if (key === 'model') await loadModel();
          if (key === 'mangrove') await loadMangrove();
          if (key === 'vision') await loadVision();
          if (key === 'accounts') await loadAccounts();
          if (key === 'overview') { await loadOverview(); await loadEvents(); }
        } catch (err) {
          toast(`加载失败：${err.message}`, 'err');
        }
        await nextTick();
        resizeCharts();
      }

      const historyDeviceOptions = computed(() => devices.value.filter((d) => d.type === 'buoy'));

      let resizeTimer = null;
      window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resizeCharts, 160);
      });

      onMounted(async () => {
        if (logged.value) {
          try {
            const me = await api.me();
            user.value = me;
            await boot();
          } catch (err) {
            store.clear();
            logged.value = false;
          }
        }
      });

      return {
        logged, user, loginForm, view, loading, VIEWS,
        overview, summary, deviceStats, regions, devices, deviceTotal, deviceQuery, selectedDevice, deviceDetail,
        metricsMeta, historyQuery, historyResult, historyBusy, historyDeviceOptions,
        alerts, alertTotal, alertQuery, alertStats, handlingAlert, handleNote,
        patrolTasks, patrolSummary, patrolReport, patrolBusy, patrolDetail, patrolQuery, patrolForm,
        loadPatrol, createPatrol, openPatrol, patrolPriorityTag, patrolStatusTag,
        modelInfo, modelConfigs, modelRules, thresholds,
        mangroveRegions, mangroveStats, mangroveTrend, mangroveAnalysis, mangroveBusy,
        visionStatus, visionCameras, visionSamples, visionForm, visionConf, visionResult, visionScan,
        visionImage,
        visionModels, visionModelBusy, switchVisionModel,
        visionBusy, visionScanBusy, selectedSample, loadVision, runVisionDetect, onVisionUpload,
        runVisionScan, raiseVisionAlert,
        users, activeUsers, auditLogs, newUser, passwordForm, events, volume,
        METRIC_ORDER, METRIC_META, fmt, icon, metricStatus,
        doLogin, logout, loadDevices, openDevice, runDeviceCommand, changeInterval,
        loadHistory, exportCsv, loadAlerts, scanAlerts, openHandle, submitHandle,
        loadModel, saveThresholds, reloadModel, loadMangrove, runMangroveAnalysis,
        loadAccounts, createUser, toggleUser, changePassword, switchView,
        navLeft: NAV_LEFT, navRight: NAV_RIGHT, roleLabel, accountTab, openScreen,
      };
    },
  }).mount('#console');
})();
