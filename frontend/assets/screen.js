/* 深蓝智护 · 数字孪生可视化大屏
   左侧：数据源状态 / 实时水质 / 水质曲线 / 告警统计
   中间：连江海域三维沙盘（浮标点位、网箱区、红树林、通信弧线、扫描环）
   右侧：赤潮风险 / 贡献因子 / 生态占比 / 实时告警流
   底栏：系统状态与滚动事件
*/
const { api, store, fmt, icon, toast, createRealtime, mountScreenScale, clamp, metricStatus, METRIC_ORDER, METRIC_META } = window.DeepBlue;

const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } = Vue;

/* ------------------------------------------------------------------ */
/* 站位基准数据（接口不可用时用于三维沙盘骨架，与后端 data/sites.py 一致）  */
/* ------------------------------------------------------------------ */
const FALLBACK_SITES = [
  { code: 'LJ-HQ-B01-B', name: '黄湾屿海洋牧场', x: 4.90, z: 4.25, region: '黄岐半岛海域' },
  { code: 'LJ-HQ-B02-B', name: '黄岐湾中部', x: 5.42, z: 2.75, region: '黄岐半岛海域' },
  { code: 'LJ-GW-B01-B', name: '官坞海带育苗区', x: -0.52, z: 5.75, region: '定海湾' },
  { code: 'LJ-DH-B01-B', name: '定海湾中部', x: 0.35, z: 5.23, region: '定海湾' },
  { code: 'LJ-DH-P01-B', name: '定海湾深远海平台', x: 0.52, z: 5.36, region: '定海湾' },
  { code: 'LJ-TL-B01-B', name: '苔菉北茭外海', x: 7.58, z: 0.52, region: '苔菉北茭海域' },
  { code: 'LJ-DLD-B01-B', name: '东洛岛养殖平台', x: 10.62, z: 0.52, region: '东洛岛深远海养殖区' },
  { code: 'LJ-MB-B01-B', name: '马鼻浮曦红树林', x: -5.55, z: -3.14, region: '马鼻镇浮曦村红树林' },
  { code: 'LJ-PK-B01-B', name: '松坞敖江口红树林', x: -10.31, z: 6.15, region: '浦口镇松坞村敖江口红树林' },
  { code: 'LJ-LYW-B01-B', name: '鉴江松山红树林核心区', x: -4.42, z: -6.21, region: '罗源湾鉴江—松山红树林集中区' },
];

const CAGE_ZONES = [
  { name: '黄湾屿海洋牧场网箱区', x: 5.1, z: 4.0, w: 3.6, d: 2.6 },
  { name: '定海湾深远海平台养殖区', x: 0.5, z: 5.2, w: 3.0, d: 2.2 },
  { name: '官坞海带养殖示范区', x: -0.6, z: 5.6, w: 2.6, d: 2.0 },
];

const MANGROVE_ZONES = [
  { name: '罗源湾鉴江—松山红树林', x: -4.42, z: -6.21, w: 4.2, d: 2.4, area: 320.0 },
  { name: '马鼻浮曦村红树林', x: -5.55, z: -3.14, w: 2.8, d: 1.6, area: 96.4 },
  { name: '松坞敖江口红树林', x: -10.31, z: 6.15, w: 2.4, d: 1.6, area: 43.8 },
];

const VIEW_MODES = [
  { key: 'overview', label: '总览', icon: 'layers' },
  { key: 'water', label: '水质监测', icon: 'droplet' },
  { key: 'redtide', label: '赤潮预警', icon: 'alert' },
  { key: 'mangrove', label: '红树林', icon: 'leaf' },
  { key: 'device', label: '设备状态', icon: 'radio' },
];

/* ------------------------------------------------------------------ */
/* 三维沙盘                                                            */
/* ------------------------------------------------------------------ */
const MAP_BOUNDS = { west: 119.18, east: 120.38, south: 26.02, north: 26.52 };
const MAP_WORLD = {
  west: (MAP_BOUNDS.west - 119.85) * 43.333,
  east: (MAP_BOUNDS.east - 119.85) * 43.333,
  north: (26.37 - MAP_BOUNDS.north) * 65.385,
  south: (26.37 - MAP_BOUNDS.south) * 65.385,
};
MAP_WORLD.width = MAP_WORLD.east - MAP_WORLD.west;
MAP_WORLD.depth = MAP_WORLD.south - MAP_WORLD.north;
MAP_WORLD.cx = (MAP_WORLD.west + MAP_WORLD.east) / 2;
MAP_WORLD.cz = (MAP_WORLD.north + MAP_WORLD.south) / 2;

function createSandbox(container) {
  const THREE = window.THREE;
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x04101f, 0.0075);

  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 900);
  // 俯瞰视角自东南方向望向黄岐半岛—定海湾岸线，让大陆位于左侧、开阔海域位于右侧
  const OVERVIEW_CAMERA = { x: MAP_WORLD.cx + 11, y: 34, z: MAP_WORLD.cz + 34 };
  const OVERVIEW_TARGET = new THREE.Vector3(MAP_WORLD.cx - 5, 0, MAP_WORLD.cz - 5);
  camera.position.set(OVERVIEW_CAMERA.x, OVERVIEW_CAMERA.y, OVERVIEW_CAMERA.z);
  camera.lookAt(OVERVIEW_TARGET);

  /* 轨道相机：以球坐标绕观测点旋转，支持左键拖拽旋转与滚轮缩放 */
  const orbit = {
    theta: 0,
    phi: 1,
    radius: 1,
    target: OVERVIEW_TARGET.clone(),
    minPhi: 0.34,
    maxPhi: 1.32,
    minRadius: 20,
    maxRadius: 118,
    dragging: false,
    lastX: 0,
    lastY: 0,
    moved: 0,
  };
  const orbitHome = { theta: 0, phi: 1, radius: 1 };
  let fly = null;

  function applyOrbit() {
    const r = orbit.radius;
    const sp = Math.sin(orbit.phi);
    camera.position.set(
      orbit.target.x + r * sp * Math.sin(orbit.theta),
      orbit.target.y + r * Math.cos(orbit.phi),
      orbit.target.z + r * sp * Math.cos(orbit.theta)
    );
    camera.lookAt(orbit.target);
  }

  // 由默认俯瞰机位反解球坐标，保证初始视角与设计稿完全一致
  (function initOrbit() {
    const dx = OVERVIEW_CAMERA.x - OVERVIEW_TARGET.x;
    const dy = OVERVIEW_CAMERA.y - OVERVIEW_TARGET.y;
    const dz = OVERVIEW_CAMERA.z - OVERVIEW_TARGET.z;
    orbit.radius = Math.sqrt(dx * dx + dy * dy + dz * dz);
    orbit.phi = Math.acos(clamp(dy / orbit.radius, -1, 1));
    orbit.theta = Math.atan2(dx, dz);
    orbitHome.theta = orbit.theta;
    orbitHome.phi = orbit.phi;
    orbitHome.radius = orbit.radius;
    applyOrbit();
  })();

  // 仅在校验脚本需要读数时保留绘制缓冲，正常运行关闭以降低合成开销
  const CAPTURE_MODE = new URLSearchParams(window.location.search).has('capture');
  const dpr = window.devicePixelRatio || 1;
  const renderer = new THREE.WebGLRenderer({
    antialias: dpr <= 1.01,
    alpha: true,
    preserveDrawingBuffer: CAPTURE_MODE,
    powerPreference: 'high-performance',
  });
  const BASE_PIXEL_RATIO = Math.min(dpr, 1.6);
  let qualityScale = 1;   // 自适应渲染倍率：帧率不足时自动下调
  let lowStreak = 0;
  let highStreak = 0;
  renderer.setPixelRatio(BASE_PIXEL_RATIO);
  renderer.setClearColor(0x000000, 0);
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0x9fdcff, 0.74));
  const key = new THREE.DirectionalLight(0xffffff, 0.70);
  key.position.set(14, 26, 12);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x33e1ff, 0.42);
  rim.position.set(-16, 12, -14);
  scene.add(rim);

  /* 海面：顶点波动 + 深浅渐变 */
  const seaGeo = new THREE.PlaneGeometry(MAP_WORLD.width + 4, MAP_WORLD.depth + 4, 150, 96);
  const seaMat = new THREE.ShaderMaterial({
    transparent: true,
    uniforms: {
      uTime: { value: 0 },
      uDeep: { value: new THREE.Color(0x052f55) },
      uShallow: { value: new THREE.Color(0x1b9bcc) },
      uHighlight: { value: new THREE.Color(0x33e1ff) },
    },
    vertexShader: `
      uniform float uTime;
      varying vec2 vUv;
      varying float vWave;
      void main() {
        vUv = uv;
        vec3 p = position;
        float w = sin(p.x * 0.32 + uTime * 0.9) * 0.34
                + cos(p.y * 0.26 + uTime * 0.72) * 0.28
                + sin((p.x + p.y) * 0.17 + uTime * 0.45) * 0.22;
        p.z += w;
        vWave = w;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }`,
    fragmentShader: `
      uniform vec3 uDeep;
      uniform vec3 uShallow;
      uniform vec3 uHighlight;
      uniform float uTime;
      varying vec2 vUv;
      varying float vWave;
      void main() {
        // 海面只叠加菲涅耳光泽、流动网格与浪脊高光，真实水深渐变由底图提供，
        // 因此这里保持低不透明度，避免遮挡海底地形影像。
        float grid = smoothstep(0.988, 1.0, max(
          abs(fract(vUv.x * 60.0 + uTime * 0.05) - 0.5),
          abs(fract(vUv.y * 40.0 - uTime * 0.04) - 0.5)
        ));
        float crest = smoothstep(0.30, 0.95, vWave);
        float sheen = crest * 0.55 + grid * 0.75;
        vec3 col = uHighlight * sheen + uShallow * 0.18;
        gl_FragColor = vec4(col, 0.14 + sheen * 0.42);
      }`,
  });
  const sea = new THREE.Mesh(seaGeo, seaMat);
  sea.rotation.x = -Math.PI / 2;
  sea.position.set(MAP_WORLD.cx, 0.34, MAP_WORLD.cz);
  scene.add(sea);

  /* 真实卫星影像 + SRTM 高程构建的连江海陆地形 */
  let terrain = null;
  let heightMap = null;
  const terrainTexture = new THREE.TextureLoader().load('/assets/geo/lianjiang_terrain.jpg');
  terrainTexture.colorSpace = THREE.SRGBColorSpace;
  terrainTexture.anisotropy = renderer.capabilities.getMaxAnisotropy();
  const coastTexture = new THREE.TextureLoader().load('/assets/geo/lianjiang_coast.png');
  coastTexture.anisotropy = renderer.capabilities.getMaxAnisotropy();

  function buildTerrain() {
    if (!heightMap || terrain) return;
    const cols = 180;
    const rows = 120;
    const geometry = new THREE.PlaneGeometry(MAP_WORLD.width, MAP_WORLD.depth, cols, rows);
    const positions = geometry.attributes.position;
    for (let i = 0; i < positions.count; i += 1) {
      const lx = positions.getX(i);
      const ly = positions.getY(i);
      const u = lx / MAP_WORLD.width + 0.5;
      const v = ly / MAP_WORLD.depth + 0.5;
      const px = clamp(Math.round(u * (heightMap.width - 1)), 0, heightMap.width - 1);
      const py = clamp(Math.round((1 - v) * (heightMap.height - 1)), 0, heightMap.height - 1);
      const idx = (py * heightMap.width + px) * 4;
      const elevation = heightMap.data[idx] / 255 * 1200;
      // 地形竖向比例控制在真实比例附近，避免山体变成尖刺
      const height = elevation <= 2 ? -0.42 : 0.06 + Math.pow(elevation, 1.04) * 0.0026;
      positions.setZ(i, height);
    }
    geometry.computeVertexNormals();
    terrain = new THREE.Mesh(
      geometry,
      // 卫星影像地形无需 PBR 高光，改用 Lambert 光照模型显著降低逐像素开销
      new THREE.MeshLambertMaterial({
        map: terrainTexture,
        color: new THREE.Color(0xffffff),
        emissive: new THREE.Color(0x123c58),
        emissiveIntensity: 0.45,
      })
    );
    terrain.rotation.x = -Math.PI / 2;
    terrain.position.set(MAP_WORLD.cx, 0, MAP_WORLD.cz);
    scene.add(terrain);
    const coast = new THREE.Mesh(
      new THREE.PlaneGeometry(MAP_WORLD.width, MAP_WORLD.depth),
      new THREE.MeshBasicMaterial({
        map: coastTexture,
        transparent: true,
        opacity: 0.62,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      })
    );
    coast.rotation.x = -Math.PI / 2;
    coast.position.set(MAP_WORLD.cx, 0.54, MAP_WORLD.cz);
    scene.add(coast);
  }

  const heightImage = new Image();
  heightImage.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = heightImage.width;
    canvas.height = heightImage.height;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(heightImage, 0, 0);
    heightMap = ctx.getImageData(0, 0, canvas.width, canvas.height);
    buildTerrain();
  };
  heightImage.src = '/assets/geo/lianjiang_height8.png';

  /* 海底参考网格 */
  const grid = new THREE.GridHelper(MAP_WORLD.width, 52, 0x1d5f83, 0x0e3b56);
  grid.scale.z = MAP_WORLD.depth / MAP_WORLD.width;
  grid.position.set(MAP_WORLD.cx, 0.24, MAP_WORLD.cz);
  grid.material.transparent = true;
  grid.material.opacity = 0.12;
  scene.add(grid);

  /* 网箱养殖区 */
  const cageGroup = new THREE.Group();
  CAGE_ZONES.forEach((zone) => {
    const box = new THREE.Mesh(
      new THREE.BoxGeometry(zone.w, 0.5, zone.d),
      new THREE.MeshBasicMaterial({ color: 0x33e1ff, transparent: true, opacity: 0.14 })
    );
      box.position.set(zone.x, 0.62, zone.z);
    cageGroup.add(box);
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(box.geometry),
      new THREE.LineBasicMaterial({ color: 0x33e1ff, transparent: true, opacity: 0.75 })
    );
    edges.position.copy(box.position);
    cageGroup.add(edges);
    // 网箱格栅
    for (let i = -1; i <= 1; i += 1) {
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(zone.x + (i * zone.w) / 3.2, 0.88, zone.z - zone.d / 2),
            new THREE.Vector3(zone.x + (i * zone.w) / 3.2, 0.88, zone.z + zone.d / 2),
        ]),
        new THREE.LineBasicMaterial({ color: 0x7ef0d2, transparent: true, opacity: 0.5 })
      );
      cageGroup.add(line);
    }
  });
  scene.add(cageGroup);

  /* 红树林区块 */
  const mangroveGroup = new THREE.Group();
  MANGROVE_ZONES.forEach((zone) => {
    const patch = new THREE.Mesh(
      new THREE.PlaneGeometry(zone.w, zone.d, 26, 16),
      new THREE.MeshLambertMaterial({ color: 0x29e7a0, transparent: true, opacity: 0.42 })
    );
    const p = patch.geometry.attributes.position;
    for (let i = 0; i < p.count; i += 1) {
      p.setZ(i, 0.35 + Math.sin(p.getX(i) * 1.6) * 0.12 + Math.cos(p.getY(i) * 2.1) * 0.1);
    }
    patch.geometry.computeVertexNormals();
    patch.rotation.x = -Math.PI / 2;
    patch.position.set(zone.x, 0.72, zone.z);
    mangroveGroup.add(patch);
    const outline = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.PlaneGeometry(zone.w, zone.d)),
      new THREE.LineBasicMaterial({ color: 0x7ef0d2, transparent: true, opacity: 0.6 })
    );
    outline.rotation.x = -Math.PI / 2;
    outline.position.set(zone.x, 0.78, zone.z);
    mangroveGroup.add(outline);
  });
  scene.add(mangroveGroup);

  /* 浮标图层（由数据驱动重建） */
  const buoyGroup = new THREE.Group();
  scene.add(buoyGroup);
  const buoyMeshes = [];

  const arcGroup = new THREE.Group();
  scene.add(arcGroup);

  const ringGroup = new THREE.Group();
  scene.add(ringGroup);

  const tooltipEl = document.getElementById('sandboxTip');
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let hovered = null;

  function labelSprite(text, color) {
    const canvas = document.createElement('canvas');
    const scale = 2;
    canvas.width = 320 * scale;
    canvas.height = 56 * scale;
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);
    // 胶囊卡片 + 指向锚点的三角指针，贴合参考大屏的悬浮标注样式
    const r = 7;
    const top = 2;
    const bottom = 42;
    const left = 2;
    const right = 318;
    ctx.beginPath();
    ctx.moveTo(left + r, top);
    ctx.lineTo(right - r, top);
    ctx.quadraticCurveTo(right, top, right, top + r);
    ctx.lineTo(right, bottom - r);
    ctx.quadraticCurveTo(right, bottom, right - r, bottom);
    ctx.lineTo(172, bottom);
    ctx.lineTo(160, bottom + 11);
    ctx.lineTo(148, bottom);
    ctx.lineTo(left + r, bottom);
    ctx.quadraticCurveTo(left, bottom, left, bottom - r);
    ctx.lineTo(left, top + r);
    ctx.quadraticCurveTo(left, top, left + r, top);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, top, 0, bottom);
    grad.addColorStop(0, 'rgba(10,42,70,0.94)');
    grad.addColorStop(1, 'rgba(4,18,34,0.90)');
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    ctx.stroke();
    // 左侧状态光点
    ctx.globalAlpha = 0.28;
    ctx.beginPath();
    ctx.arc(24, 22, 9, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.beginPath();
    ctx.arc(24, 22, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#e8f7ff';
    ctx.font = '600 20px "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 172, 23);
    const texture = new THREE.CanvasTexture(canvas);
    texture.minFilter = THREE.LinearFilter;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }));
    sprite.scale.set(5.8, 1.02, 1);
    sprite.userData.baseScale = { x: 5.8, y: 1.02 };
    return sprite;
  }

  function hexToRgba(hex, alpha) {
    const c = new THREE.Color(hex);
    const r = Math.round(c.r * 255);
    const g = Math.round(c.g * 255);
    const b = Math.round(c.b * 255);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  const glowTextures = new Map();
  function glowTexture(color) {
    if (glowTextures.has(color)) return glowTextures.get(color);
    const size = 128;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0.00, hexToRgba(color, 0.85));
    grad.addColorStop(0.32, hexToRgba(color, 0.34));
    grad.addColorStop(1.00, hexToRgba(color, 0));
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    const texture = new THREE.CanvasTexture(canvas);
    texture.minFilter = THREE.LinearFilter;
    glowTextures.set(color, texture);
    return texture;
  }

  function glowSprite(color, size) {
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture(color),
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      opacity: 0.75,
    }));
    sprite.scale.set(size, size, 1);
    return sprite;
  }

  // 海域地名标注：黄岐半岛、定海湾、罗源湾、敖江口等
  const MAP_POIS = [
    { name: '黄岐半岛', x: 6.1, z: 1.1, color: '#9fe7ff' },
    { name: '定海湾', x: 0.4, z: 7.4, color: '#7ef0d2' },
    { name: '罗源湾', x: -1.4, z: -5.2, color: '#7ef0d2' },
    { name: '敖江口', x: -9.2, z: 9.4, color: '#ffd166' },
    { name: '马鼻浮曦红树林', x: -6.4, z: -2.2, color: '#29e7a0' },
    { name: '东洛岛深远海养殖区', x: 10.6, z: -0.6, color: '#33e1ff' },
  ];

  const poiGroup = new THREE.Group();
  scene.add(poiGroup);
  MAP_POIS.forEach((poi, index) => {
    const lift = 3.4 + (index % 3) * 0.7;
    const stem = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(poi.x, 0.55, poi.z),
        new THREE.Vector3(poi.x, lift - 0.5, poi.z),
      ]),
      new THREE.LineBasicMaterial({ color: poi.color, transparent: true, opacity: 0.42 })
    );
    poiGroup.add(stem);
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(0.16, 12, 12),
      new THREE.MeshBasicMaterial({ color: poi.color, transparent: true, opacity: 0.95 })
    );
    dot.position.set(poi.x, 0.6, poi.z);
    poiGroup.add(dot);
    const label = labelSprite(poi.name, poi.color);
    label.position.set(poi.x, lift + 0.6, poi.z);
    poiGroup.add(label);
  });

  // 释放被替换对象占用的几何体、材质与贴图，避免长时间运行时显存持续增长
  function disposeObject(root) {
    root.traverse((node) => {
      if (node.geometry) node.geometry.dispose();
      const material = node.material;
      if (!material) return;
      (Array.isArray(material) ? material : [material]).forEach((m) => {
        if (m.map) m.map.dispose();
        m.dispose();
      });
    });
  }

  function clearGroup(group) {
    for (let i = group.children.length - 1; i >= 0; i -= 1) {
      const child = group.children[i];
      group.remove(child);
      disposeObject(child);
    }
  }

  let buoySignature = '';
  function buildBuoys(items, colorOf) {
    // 点位集合与配色未变化时直接复用现有网格，避免每次数据上报都重建几何体与贴图
    const signature = items.map((item) => `${item.code}:${colorOf(item)}`).join('|');
    if (signature === buoySignature && buoyGroup.children.length) return;
    buoySignature = signature;
    clearGroup(buoyGroup);
    buoyMeshes.length = 0;
    // 高风险点位排在前面，优先获得常驻名称标牌
    const ranked = items.slice().sort((a, b) => (b.risk_level || 0) - (a.risk_level || 0));
    ranked.forEach((item, index) => {
      const labelLift = 3.3 + (index % 3) * 0.82;
      const color = colorOf(item);
      const group = new THREE.Group();
      group.position.set(item.x, 0, item.z);

      const base = new THREE.Mesh(
        new THREE.CylinderGeometry(0.42, 0.5, 0.34, 20),
        new THREE.MeshLambertMaterial({ color, emissive: color, emissiveIntensity: 0.45 })
      );
      base.position.y = 0.55;
      base.userData = { code: item.code, name: item.name, site: item };
      group.add(base);

      const mast = new THREE.Mesh(
        new THREE.CylinderGeometry(0.06, 0.06, 1.0, 8),
        new THREE.MeshLambertMaterial({ color: 0xbff0ff, emissive: 0x33e1ff, emissiveIntensity: 0.4 })
      );
      mast.position.y = 1.2;
      group.add(mast);
      buoyMeshes.push(base);

      const halo = new THREE.Mesh(
        new THREE.RingGeometry(0.75, 1.0, 32),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
      );
      halo.rotation.x = -Math.PI / 2;
      halo.position.y = 0.64;
      halo.userData = { base };
      group.add(halo);

      // 水面光晕与垂直光柱：夜间大屏下的悬浮信标效果
      const glow = glowSprite(color, 3.6);
      glow.position.y = 0.72;
      glow.material.opacity = 0.5;
      glow.userData = { glow: true, phase: index * 0.7 };
      group.add(glow);

      const beam = new THREE.Mesh(
        new THREE.CylinderGeometry(0.05, 0.11, 2.2, 10, 1, true),
        new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity: 0.28,
          side: THREE.DoubleSide,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        })
      );
      beam.position.y = 1.72;
      group.add(beam);

      // 仅高优先级点位常驻显示名称标牌，其余点位保留信标与悬停提示，避免互相遮挡
      if (index < 4 || (item.risk_level || 0) >= 3) {
        const label = labelSprite(item.name, color);
        label.position.set(index % 2 ? 1.7 : -1.7, labelLift, 0);
        group.add(label);
      }

      buoyGroup.add(group);
    });
  }

  function buildArcs(items) {
    clearGroup(arcGroup);
    const hub = new THREE.Vector3(0, 18, -6);
    items.forEach((item, index) => {
      const start = new THREE.Vector3(item.x, 1.2, item.z);
      const mid = start.clone().lerp(hub, 0.5);
      mid.y += 7 + (index % 3);
      const curve = new THREE.QuadraticBezierCurve3(start, mid, hub);
      const points = curve.getPoints(40);
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(points),
        new THREE.LineDashedMaterial({ color: 0x33e1ff, transparent: true, opacity: 0.32, dashSize: 0.7, gapSize: 1.1 })
      );
      line.computeLineDistances();
      arcGroup.add(line);
    });
    const hubMesh = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.8, 1),
      new THREE.MeshBasicMaterial({ color: 0x33e1ff, wireframe: true, transparent: true, opacity: 0.7 })
    );
    hubMesh.position.copy(hub);
    hubMesh.name = 'commHub';
    arcGroup.add(hubMesh);
  }

  function buildScanRings(items) {
    clearGroup(ringGroup);
    items.slice(0, 3).forEach((item, index) => {
      for (let k = 0; k < 2; k += 1) {
        const ring = new THREE.Mesh(
          new THREE.RingGeometry(0.9 + k * 0.5, 1.05 + k * 0.5, 48),
          new THREE.MeshBasicMaterial({ color: item.color, transparent: true, opacity: 0.55 - k * 0.18, side: THREE.DoubleSide })
        );
        ring.rotation.x = -Math.PI / 2;
        ring.position.set(item.x, 0.66 + k * 0.05, item.z);
        ring.userData = { phase: index * 0.9 + k * 0.45, base: 0.9 + k * 0.5 };
        ringGroup.add(ring);
      }
    });
  }

  function setView(mode) {
    const seaVisible = mode !== 'device';
    sea.visible = seaVisible;
    cageGroup.visible = mode === 'overview' || mode === 'water' || mode === 'redtide';
    mangroveGroup.visible = mode === 'overview' || mode === 'mangrove' || mode === 'redtide';
    arcGroup.visible = mode === 'overview' || mode === 'device';
    poiGroup.visible = mode !== 'device';
  }

  // 定位到某个点位或回到全域视角：交给渲染循环插值到目标轨道，与拖拽互不冲突
  function focusOn(target) {
    if (target) {
      fly = {
        target: new THREE.Vector3(target.x * 0.55 - 2, 0, target.z * 0.55 - 2),
        radius: 30,
        phi: 0.86,
        theta: orbit.theta,
      };
    } else {
      fly = {
        target: OVERVIEW_TARGET.clone(),
        radius: orbitHome.radius,
        phi: orbitHome.phi,
        theta: orbitHome.theta,
      };
    }
  }

  /* 交互：拖拽旋转、滚轮缩放、悬停提示与点击选中 */
  function onPointerDown(event) {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    fly = null;
    orbit.dragging = true;
    orbit.moved = 0;
    orbit.lastX = event.clientX;
    orbit.lastY = event.clientY;
    renderer.domElement.style.cursor = 'grabbing';
    event.preventDefault();
    if (renderer.domElement.setPointerCapture) {
      try { renderer.domElement.setPointerCapture(event.pointerId); } catch (err) { /* 部分环境不支持指针捕获，忽略即可 */ }
    }
  }

  function onPointerUp(event) {
    if (!orbit.dragging) return;
    orbit.dragging = false;
    if (!hovered) renderer.domElement.style.cursor = 'grab';
    if (renderer.domElement.releasePointerCapture) {
      try { renderer.domElement.releasePointerCapture(event.pointerId); } catch (err) { /* 忽略 */ }
    }
  }

  function onWheel(event) {
    event.preventDefault();
    fly = null;
    orbit.radius = clamp(orbit.radius * (1 + event.deltaY * 0.0012), orbit.minRadius, orbit.maxRadius);
    applyOrbit();
  }

  function onPointerMove(event) {
    if (orbit.dragging) {
      const dx = event.clientX - orbit.lastX;
      const dy = event.clientY - orbit.lastY;
      orbit.lastX = event.clientX;
      orbit.lastY = event.clientY;
      orbit.moved += Math.abs(dx) + Math.abs(dy);
      orbit.theta -= dx * 0.0052;
      orbit.phi = clamp(orbit.phi - dy * 0.0042, orbit.minPhi, orbit.maxPhi);
      applyOrbit();
      if (tooltipEl) tooltipEl.style.display = 'none';
      return;
    }
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(buoyMeshes, false);
    const hit = hits[0] ? hits[0].object : null;
    if (hit !== hovered) {
      hovered = hit;
      renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
      if (hit && tooltipEl) {
        const d = hit.userData;
        tooltipEl.style.display = 'block';
        tooltipEl.style.left = `${event.clientX - rect.left + 14}px`;
        tooltipEl.style.top = `${event.clientY - rect.top + 14}px`;
        tooltipEl.innerHTML = `<b>${d.name}</b><span class="mono">${d.code}</span>`;
      } else if (tooltipEl) {
        tooltipEl.style.display = 'none';
      }
    } else if (hit && tooltipEl) {
      tooltipEl.style.left = `${event.clientX - rect.left + 14}px`;
      tooltipEl.style.top = `${event.clientY - rect.top + 14}px`;
    }
  }

  let clickHandler = null;
  function onClick(event) {
    // 拖拽后不触发点位选中，避免旋转视角时误选设备
    if (orbit.moved > 6) { orbit.moved = 0; return; }
    orbit.moved = 0;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(buoyMeshes, false);
    if (hits[0] && clickHandler) clickHandler(hits[0].object.userData.code);
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown);
  renderer.domElement.addEventListener('pointermove', onPointerMove);
  renderer.domElement.addEventListener('pointerup', onPointerUp);
  renderer.domElement.addEventListener('pointercancel', onPointerUp);
  renderer.domElement.addEventListener('click', onClick);
  renderer.domElement.addEventListener('wheel', onWheel, { passive: false });

  // 图形上下文可能被系统回收（长时间运行、显存紧张或后台标签页）。
  // 此处监听上下文丢失与恢复，避免渲染循环持续抛出异常。
  let contextLost = false;
  let raf = 0;
  renderer.domElement.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    contextLost = true;
    cancelAnimationFrame(raf);
  });
  renderer.domElement.addEventListener('webglcontextrestored', () => {
    contextLost = false;
    raf = requestAnimationFrame(animate);
  });

  const clock = new THREE.Clock();
  // 大屏动画限帧至 30 FPS：海面与光效依然流畅，GPU 占用与风扇噪声明显下降
  const FRAME_STEP = 1 / 30;
  let frameDebt = FRAME_STEP;
  let idle = false;
  let frames = 0;
  let fpsWindow = performance.now();
  let fps = 0;
  function animate() {
    if (contextLost) return;
    raf = requestAnimationFrame(animate);
    frameDebt += clock.getDelta();
    if (idle || frameDebt < FRAME_STEP) return;
    frameDebt = 0;
    const t = clock.elapsedTime;
    seaMat.uniforms.uTime.value = t;
    const hubMesh = arcGroup.getObjectByName('commHub');
    if (hubMesh) { hubMesh.rotation.y = t * 0.6; hubMesh.rotation.x = t * 0.3; }
    ringGroup.children.forEach((ring) => {
      const local = (t * 0.55 + ring.userData.phase) % 2.2;
      const s = ring.userData.base + local * 1.5;
      ring.scale.set(s, s, s);
      ring.material.opacity = clamp(0.55 - local * 0.24, 0.02, 0.6);
    });
    buoyGroup.children.forEach((group) => {
      group.children.forEach((child) => {
        if (child.userData && child.userData.glow) {
          const k = 1 + Math.sin(t * 1.5 + child.userData.phase) * 0.14;
          child.scale.set(3.6 * k, 3.6 * k, 1);
          child.material.opacity = 0.38 + (k - 1) * 1.3;
        }
      });
    });
    // 镜头飞行：平滑过渡到目标海域或复位到全域视角
    if (fly) {
      const k = 0.12;
      orbit.target.lerp(fly.target, k);
      orbit.radius += (fly.radius - orbit.radius) * k;
      orbit.phi += (fly.phi - orbit.phi) * k;
      let dTheta = fly.theta - orbit.theta;
      while (dTheta > Math.PI) dTheta -= Math.PI * 2;
      while (dTheta < -Math.PI) dTheta += Math.PI * 2;
      orbit.theta += dTheta * k;
      applyOrbit();
      if (orbit.target.distanceTo(fly.target) < 0.05 && Math.abs(fly.radius - orbit.radius) < 0.15) fly = null;
    }
    try {
      renderer.render(scene, camera);
      frames += 1;
      const nowMs = performance.now();
      if (nowMs - fpsWindow >= 1000) {
        fps = Math.round((frames * 1000) / (nowMs - fpsWindow));
        // 渲染限帧在 30 FPS，低于 22 FPS 视为持续掉帧，逐级降低渲染倍率；回到 28 FPS 以上再缓慢回升
        if (fps < 22) { lowStreak += 1; highStreak = 0; }
        else if (fps >= 28) { highStreak += 1; lowStreak = 0; }
        if (lowStreak >= 3 && qualityScale > 0.66) {
          qualityScale = Math.max(0.66, qualityScale - 0.17);
          lowStreak = 0;
          renderer.setPixelRatio(BASE_PIXEL_RATIO * qualityScale);
        } else if (highStreak >= 8 && qualityScale < 1) {
          qualityScale = Math.min(1, qualityScale + 0.085);
          highStreak = 0;
          renderer.setPixelRatio(BASE_PIXEL_RATIO * qualityScale);
        }
        frames = 0;
        fpsWindow = nowMs;
      }
    } catch (err) {
      contextLost = true;
      cancelAnimationFrame(raf);
      window.console.warn('[sandbox] 三维渲染已暂停：', err && err.message);
    }
  }
  const onVisibility = () => {
    idle = document.hidden;
    if (!idle) frameDebt = FRAME_STEP;
  };
  document.addEventListener('visibilitychange', onVisibility);
  animate();

  function resize() {
    const w = container.clientWidth || 1;
    const h = container.clientHeight || 1;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  resize();

  return {
    resize, setView, focusOn, buildBuoys, buildArcs, buildScanRings,
    debug() {
      const projects = buoyGroup.children.map((g) => {
        const v = g.position.clone().project(camera);
        return { x: +v.x.toFixed(3), y: +v.y.toFixed(3), z: +v.z.toFixed(3), world: [+g.position.x.toFixed(2), 0, +g.position.z.toFixed(2)] };
      });
      const terrainBox = terrain ? new THREE.Box3().setFromObject(terrain) : null;
      const seaBox = new THREE.Box3().setFromObject(sea);
      return {
        camera: camera.position.toArray().map((v) => +v.toFixed(2)),
        buoyCount: buoyGroup.children.length,
        arcs: arcGroup.children.length,
        projects,
        terrainBox: terrainBox ? { min: terrainBox.min.toArray().map((v) => +v.toFixed(2)), max: terrainBox.max.toArray().map((v) => +v.toFixed(2)) } : null,
        seaVisible: sea.visible,
        orbit: { theta: +orbit.theta.toFixed(3), phi: +orbit.phi.toFixed(3), radius: +orbit.radius.toFixed(2) },
        fps,
        pixelRatio: renderer.getPixelRatio(),
        qualityScale,
        seaBox: { min: seaBox.min.toArray().map((v) => +v.toFixed(2)), max: seaBox.max.toArray().map((v) => +v.toFixed(2)) },
      };
    },
    onClickBuoy(fn) { clickHandler = fn; },
    dispose() {
      cancelAnimationFrame(raf);
      contextLost = true;
      document.removeEventListener('visibilitychange', onVisibility);
      renderer.domElement.removeEventListener('pointerdown', onPointerDown);
      renderer.domElement.removeEventListener('pointermove', onPointerMove);
      renderer.domElement.removeEventListener('pointerup', onPointerUp);
      renderer.domElement.removeEventListener('pointercancel', onPointerUp);
      renderer.domElement.removeEventListener('click', onClick);
      renderer.domElement.removeEventListener('wheel', onWheel);
      renderer.dispose();
      container.innerHTML = '';
    },
  };
}

/* ------------------------------------------------------------------ */
/* 大屏应用                                                            */
/* ------------------------------------------------------------------ */
createApp({
  setup() {
    const loading = ref(true);
    const mode = ref('overview');
    const overview = ref(null);
    const latest = ref([]);
    const alerts = ref([]);
    const riskPoints = ref([]);
    const selected = ref(null);
    const activeAlert = ref(null);
    const realtimeStatus = ref('connecting');
    const lastTick = ref(new Date().toISOString());
    const flash = ref(false);
    const clock = reactive({ time: '', date: '', tick: 0 });

    /* 海洋生态智能体：问答状态、工具调用轨迹与大屏高亮联动 */
    const AGENT_EXAMPLES = [
      '现在平台整体情况怎么样？',
      '黄岐湾中部浮标最新的水质如何？',
      '未来 48 小时赤潮风险最高的点位在哪里？',
      '最近 24 小时有哪些告警还没处理？',
    ];
    const agentBody = ref(null);
    const agent = reactive({
      open: false,
      busy: false,
      input: '',
      messages: [],
      model: '',
      toolCount: 0,
      degraded: false,
      statusText: '待命',
      statusKind: '',
    });

    const CHART_IDS = {
      water: 'chartWater',
      alert: 'chartAlert',
      risk: 'chartRisk',
      factors: 'chartFactors',
      eco: 'chartEco',
      mangrove: 'chartMangrove',
    };
    const charts = {};
    const chartMeta = {};

    const metrics = computed(() => {
      const avg = (overview.value && overview.value.averages) || {};
      return METRIC_ORDER.map((key) => ({
        key,
        label: METRIC_META[key].label,
        unit: METRIC_META[key].unit,
        color: METRIC_META[key].color,
        value: avg[key],
        status: metricStatus(key, avg[key]),
      }));
    });

    const summary = computed(() => (overview.value && overview.value.summary) || {});
    const riskSummary = computed(() => (overview.value && overview.value.risk) || { distribution: [], points: [] });
    const mangrove = computed(() => (overview.value && overview.value.mangrove) || { items: [] });
    const ecoShare = computed(() => (overview.value && overview.value.eco_share) || []);

    const riskLevel = computed(() => riskSummary.value.overall_level || 1);
    /* 指标相对 24 小时前的变化幅度，用于趋势标签 */
    function trendOf(key) {
      const series = (overview.value && overview.value.trend && overview.value.trend.series[key]) || [];
      const values = series.filter((v) => v !== null && v !== undefined);
      if (values.length < 2) return { cls: 'flat', text: '稳定' };
      const delta = values[values.length - 1] - values[0];
      const threshold = Math.abs(values[0]) * 0.03 + 0.01;
      if (delta > threshold) return { cls: 'up', text: `+${delta.toFixed(2)}` };
      if (delta < -threshold) return { cls: 'down', text: delta.toFixed(2) };
      return { cls: 'flat', text: '稳定' };
    }

    const deviceTypes = computed(() => {
      const s = summary.value;
      const buoys = s.buoy_total || 0;
      const total = s.device_total || 0;
      return [
        { key: 'buoy', label: '多参数水质浮标', count: buoys, online: s.buoy_online || 0, icon: 'anchor' },
        { key: 'edge', label: '边缘网关 / 摄像头', count: Math.max(0, total - buoys), online: Math.max(0, (s.device_online || 0) - (s.buoy_online || 0)), icon: 'cpu' },
      ];
    });

    const riskProbability = computed(() => {
      const points = riskPoints.value;
      if (!points.length) return 0;
      return Math.max(...points.map((p) => p.probability || 0));
    });

    /* 环形仪表中心的概率读数由 HTML 层渲染，避免与 ECharts 内建文字重叠 */
    const riskPercent = computed(() => Math.round(riskProbability.value * 100));

    const banner = computed(() => {
      const info = riskSummary.value;
      const top = (riskPoints.value[0] || null);
      if (mode.value === 'mangrove') {
        const m = mangrove.value;
        return `红树林湿地监测面积 ${fmt.num(m.total_area_ha, 1)} 公顷，加权健康度 ${fmt.num(m.average_health, 1)}（NDVI/NDWI 由 Sentinel-2 期次影像计算）`;
      }
      if (mode.value === 'water') {
        return `全域 10 个浮标平均叶绿素 a ${fmt.num(overview.value?.averages?.chlorophyll_a)} μg/L，溶解氧 ${fmt.num(overview.value?.averages?.dissolved_oxygen)} mg/L`;
      }
      if (mode.value === 'device') {
        return `在网设备 ${fmt.int(summary.value.device_total)} 台，在线率 ${fmt.pct(summary.value.online_rate)}，数据源：浮标实测 / 边缘网关补传`;
      }
      if (top && top.level >= 3) {
        return `AI 风险研判：${top.region_name || ''} 赤潮风险 ${top.level_label}，风险概率 ${fmt.pct(top.probability * 100)}，建议提前布设应急监测`;
      }
      return `${info.overall_name || '低风险'} · 全域 ${riskSummary.value.device_count || 0} 个点位参与研判，模型 ${summary.value.model_version || 'v1'}`;
    });

    const legend = computed(() => {
      if (mode.value === 'water') {
        return [
          { color: '#29e7a0', text: '叶绿素a 正常（<10 μg/L）' },
          { color: '#ffd166', text: '叶绿素a 关注（10—20 μg/L）' },
          { color: '#ff4d6d', text: '叶绿素a 越限（>20 μg/L）' },
        ];
      }
      if (mode.value === 'redtide') {
        return [
          { color: '#33e1ff', text: 'Ⅰ级 低风险' },
          { color: '#ffd166', text: 'Ⅱ级 关注' },
          { color: '#ff9f43', text: 'Ⅲ级 中高风险' },
          { color: '#ff4d6d', text: 'Ⅳ级 高风险' },
        ];
      }
      if (mode.value === 'mangrove') {
        return [
          { color: '#29e7a0', text: '红树林斑块' },
          { color: '#7ef0d2', text: '潮间带边界' },
        ];
      }
      if (mode.value === 'device') {
        return [
          { color: '#29e7a0', text: '在线' },
          { color: '#ffd166', text: '信号弱 / 电量低' },
          { color: '#ff4d6d', text: '离线或故障' },
        ];
      }
      return [
        { color: '#33e1ff', text: '多参数水质浮标' },
        { color: '#7ef0d2', text: '网箱与养殖平台' },
        { color: '#29e7a0', text: '红树林湿地' },
      ];
    });

    function colorOf(item) {
      if (mode.value === 'device') {
        if (item.status !== 'online') return '#ff4d6d';
        if ((item.battery ?? 100) < 60 || (item.signal ?? -60) < -90) return '#ffd166';
        return '#29e7a0';
      }
      if (mode.value === 'water') {
        const s = metricStatus('chlorophyll_a', item.values?.chlorophyll_a);
        return s === 'crit' ? '#ff4d6d' : s === 'warn' ? '#ffd166' : '#29e7a0';
      }
      if (mode.value === 'mangrove') return '#29e7a0';
      const risk = riskPoints.value.find((p) => p.device_code === item.code);
      if (risk) return risk.color;
      return item.status === 'online' ? '#33e1ff' : '#ff4d6d';
    }

    function sandboxItems() {
      const source = latest.value.length
        ? latest.value.map((d) => ({
            code: d.device_code,
            name: (d.site || d.region_name || d.device_name || '').replace('多参数水质浮标', ''),
            x: d.world_x ?? 0,
            z: d.world_z ?? 0,
            status: d.status,
            battery: d.battery,
            signal: d.signal,
            values: d.values,
            region: d.region_name,
            risk_level: (riskPoints.value.find((p) => p.device_code === d.device_code) || {}).level || 0,
          }))
        : FALLBACK_SITES.map((s) => ({ ...s, status: 'online', values: {} }));
      return source;
    }

    /* ---------- 图表 ---------- */
    function ensureChart(key) {
      const el = document.getElementById(CHART_IDS[key]);
      if (!el) return null;
      if (charts[key] && chartMeta[key] === el) return charts[key];
      if (charts[key]) charts[key].dispose();
      charts[key] = window.echarts.init(el, null, { renderer: 'canvas' });
      chartMeta[key] = el;
      return charts[key];
    }

    const AXIS = {
      axisLine: { lineStyle: { color: 'rgba(51,225,255,0.28)' } },
      axisLabel: { color: '#7fb3cc', fontSize: 10, fontFamily: 'Consolas, monospace' },
      splitLine: { lineStyle: { color: 'rgba(51,225,255,0.08)' } },
    };

    function renderWaterChart() {
      const chart = ensureChart('water');
      if (!chart) return;
      const trend = (overview.value && overview.value.trend) || { labels: [], series: {} };
      const series = ['chlorophyll_a', 'dissolved_oxygen', 'water_temp'].map((key) => ({
        name: METRIC_META[key].label,
        type: 'line',
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1.8, color: METRIC_META[key].color },
        itemStyle: { color: METRIC_META[key].color },
        areaStyle: key === 'chlorophyll_a'
          ? { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
              { offset: 0, color: 'rgba(41,231,160,0.34)' }, { offset: 1, color: 'rgba(41,231,160,0)' }] } }
          : undefined,
        data: trend.series[key] || [],
      }));
      chart.setOption({
        animation: false,
        grid: { left: 38, right: 12, top: 30, bottom: 22 },
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(6,28,50,0.94)', borderColor: 'rgba(51,225,255,0.4)', textStyle: { color: '#dff3ff', fontSize: 11 } },
        legend: { data: series.map((s) => s.name), textStyle: { color: '#7fb3cc', fontSize: 10 }, top: 0, itemWidth: 12, itemHeight: 8 },
        xAxis: { type: 'category', boundaryGap: false, data: trend.labels, ...AXIS },
        yAxis: { type: 'value', scale: true, ...AXIS },
        series,
      }, true);
    }

    function renderAlertChart() {
      const chart = ensureChart('alert');
      if (!chart) return;
      const trend = (overview.value && overview.value.alert_trend) || [];
      const buckets = trend.map((t) => t.bucket);
      const counts = trend.map((t) => t.count);
      const levels = (overview.value?.alerts || []).reduce((acc, a) => {
        acc[a.level] = (acc[a.level] || 0) + 1;
        return acc;
      }, {});
      chart.setOption({
        animation: false,
        grid: { left: 34, right: 12, top: 26, bottom: 20 },
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(6,28,50,0.94)', borderColor: 'rgba(51,225,255,0.4)', textStyle: { color: '#dff3ff', fontSize: 11 } },
        legend: { data: ['告警次数'], textStyle: { color: '#7fb3cc', fontSize: 10 }, top: 0, itemWidth: 12, itemHeight: 8 },
        xAxis: { type: 'category', data: buckets, ...AXIS, axisLabel: { ...AXIS.axisLabel, interval: 3 } },
        yAxis: { type: 'value', minInterval: 1, ...AXIS },
        series: [{
          name: '告警次数',
          type: 'bar',
          barWidth: '52%',
          data: counts.map((v) => ({
            value: v,
            itemStyle: { color: v > 0 ? 'rgba(255,159,67,0.85)' : 'rgba(51,225,255,0.25)', borderRadius: [3, 3, 0, 0] },
          })),
        }],
        graphic: [
          { type: 'text', right: 12, top: 2, style: { text: `累计 严重 ${levels.critical || 0} / 警告 ${levels.warning || 0}`, fill: '#7fb3cc', fontSize: 10 } },
        ],
      }, true);
    }

    function renderRiskChart() {
      const chart = ensureChart('risk');
      if (!chart) return;
      const level = riskLevel.value;
      const info = ['#33e1ff', '#ffd166', '#ff9f43', '#ff4d6d'][level - 1];
      chart.setOption({
        animation: false,
        series: [
          {
            type: 'gauge',
            startAngle: 220,
            endAngle: -40,
            min: 0,
            max: 1,
            radius: '76%',
            center: ['50%', '50%'],
            progress: { show: true, width: 10, roundCap: true, itemStyle: { color: info, shadowBlur: 10, shadowColor: info } },
            axisLine: { lineStyle: { width: 10, color: [[1, 'rgba(51,225,255,0.14)']] } },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: { show: false },
            pointer: { show: false },
            anchor: { show: false },
            detail: { show: false },
            title: { show: false },
            data: [{ value: riskProbability.value, name: `赤潮风险概率 · ${riskSummary.value.overall_name || ''}` }],
          },
        ],
      }, true);
    }

    function renderFactorsChart() {
      const chart = ensureChart('factors');
      if (!chart) return;
      const top = riskPoints.value[0];
      const factors = (top && top.top_factors || []).slice(0, 5).reverse();
      if (!factors.length) {
        chart.clear();
        chart.setOption({
          animation: false,
          title: { text: '暂无风险贡献数据', left: 'center', top: 'middle', textStyle: { color: '#567f99', fontSize: 11, fontWeight: 400 } },
        });
        return;
      }
      const max = Math.max(...factors.map((f) => f.contribution)) || 1;
      chart.setOption({
        animation: false,
        grid: { left: 92, right: 34, top: 8, bottom: 6 },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: 'rgba(6,28,50,0.94)', borderColor: 'rgba(51,225,255,0.4)', textStyle: { color: '#dff3ff', fontSize: 11 } },
        xAxis: { type: 'value', show: false, max: max * 1.15 },
        yAxis: {
          type: 'category',
          data: factors.map((f) => f.label),
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: '#a9d8ec', fontSize: 10.5 },
        },
        series: [{
          type: 'bar',
          data: factors.map((f) => ({
            value: Number(f.contribution.toFixed(4)),
            itemStyle: {
              borderRadius: [0, 3, 3, 0],
              color: f.direction === '偏高' || f.direction === '适宜'
                ? { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [
                    { offset: 0, color: 'rgba(51,225,255,0.35)' }, { offset: 1, color: '#33e1ff' }] }
                : { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [
                    { offset: 0, color: 'rgba(255,159,67,0.35)' }, { offset: 1, color: '#ff9f43' }] },
            },
          })),
          barWidth: 9,
          label: { show: true, position: 'right', color: '#dff3ff', fontSize: 10, fontFamily: 'Consolas, monospace', formatter: (p) => p.value.toFixed(3) },
        }],
      }, true);
    }

    function renderEcoChart() {
      const chart = ensureChart('eco');
      if (!chart) return;
      const data = ecoShare.value.map((d) => ({ name: d.name, value: d.value, itemStyle: { color: d.color } }));
      chart.setOption({
        animation: false,
        tooltip: {
          trigger: 'item',
          backgroundColor: 'rgba(6,28,50,0.94)',
          borderColor: 'rgba(51,225,255,0.4)',
          textStyle: { color: '#dff3ff', fontSize: 11 },
          formatter: (p) => `${p.name}<br/><b>${p.value}</b> 公顷（${p.percent}%）`,
        },
        legend: { orient: 'vertical', right: 4, top: 'middle', itemWidth: 9, itemHeight: 9, textStyle: { color: '#a9d8ec', fontSize: 10.5 }, itemGap: 9 },
        series: [{
          type: 'pie',
          radius: ['46%', '72%'],
          center: ['32%', '52%'],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: 'rgba(4,16,31,0.9)', borderWidth: 2 },
          label: { show: false },
          labelLine: { show: false },
          data,
        }],
      }, true);
    }

    function renderMangroveChart() {
      const chart = ensureChart('mangrove');
      if (!chart) return;
      const items = mangrove.value.items || [];
      if (!items.length) { chart.clear(); return; }
      chart.setOption({
        animation: false,
        grid: { left: 40, right: 18, top: 14, bottom: 24 },
        tooltip: { trigger: 'axis', backgroundColor: 'rgba(6,28,50,0.94)', borderColor: 'rgba(51,225,255,0.4)', textStyle: { color: '#dff3ff', fontSize: 11 } },
        xAxis: { type: 'category', data: items.map((i) => (i.region_name || '').slice(-4)), ...AXIS },
        yAxis: { type: 'value', min: 0, max: 100, ...AXIS },
        series: [{
          name: '健康度',
          type: 'bar',
          barWidth: '46%',
          data: items.map((i) => ({
            value: i.health_score,
            itemStyle: {
              borderRadius: [3, 3, 0, 0],
              color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
                { offset: 0, color: '#29e7a0' }, { offset: 1, color: 'rgba(41,231,160,0.22)' }] },
            },
          })),
          label: { show: true, position: 'top', color: '#dff3ff', fontSize: 10, fontFamily: 'Consolas, monospace' },
        }],
      }, true);
    }

    function renderAllCharts() {
      renderWaterChart();
      renderAlertChart();
      renderRiskChart();
      renderFactorsChart();
      renderEcoChart();
    }

    /* ---------- 数据加载 ---------- */
    async function loadOverview() {
      try {
        const data = await api.overview();
        overview.value = data;
        latest.value = data.latest || [];
        alerts.value = data.alerts || [];
        riskPoints.value = (data.risk && data.risk.points) || [];
        lastTick.value = data.generated_at;
        if (sandbox) {
          sandbox.buildBuoys(sandboxItems(), colorOf);
          sandbox.buildArcs(sandboxItems());
          sandbox.buildScanRings(riskPoints.value.filter((p) => p.level >= 3));
          sandbox.setView(mode.value);
        }
        await nextTick();
        renderAllCharts();
        renderMangroveChart();
      } catch (err) {
        toast(`大屏数据加载失败：${err.message}`, 'err');
      } finally {
        loading.value = false;
      }
    }

    /* ---------- 实时通道 ---------- */
    let realtime = null;
    function bindRealtime() {
      realtime = createRealtime({
        onStatus: (s) => { realtimeStatus.value = s; },
        onEvent: (payload) => {
          if (payload.type === 'reading') {
            const items = (payload.data && payload.data.items) || [];
            const map = new Map(latest.value.map((d) => [d.device_code, d]));
            items.forEach((item) => {
              const existing = map.get(item.device_code);
              if (existing) {
                existing.values = item.values;
                existing.ts = item.ts;
                existing.status = 'online';
                existing.age_seconds = 0;
              }
            });
            latest.value = Array.from(map.values());
            if (overview.value) {
              overview.value.averages = METRIC_ORDER.reduce((acc, key) => {
                const values = latest.value.map((d) => d.values?.[key]).filter((v) => v !== null && v !== undefined);
                acc[key] = values.length ? Number((values.reduce((a, b) => a + b, 0) / values.length).toFixed(2)) : null;
                return acc;
              }, {});
            }
            lastTick.value = payload.ts;
            if (sandbox) sandbox.buildBuoys(sandboxItems(), colorOf);
          } else if (payload.type === 'alert') {
            alerts.value = [payload.data, ...alerts.value].slice(0, 40);
            if (payload.data.level === 'critical') {
              activeAlert.value = payload.data;
              flash.value = true;
              setTimeout(() => { flash.value = false; }, 2600);
            }
            renderAlertChart();
          } else if (payload.type === 'alert_update') {
            const idx = alerts.value.findIndex((a) => a.id === payload.data.id);
            if (idx >= 0) alerts.value.splice(idx, 1, payload.data);
          } else if (payload.type === 'system') {
            events.push({ message: payload.data.message, created_at: payload.ts });
          }
        },
      });
    }

    const events = reactive([]);

    /* ---------- 交互 ---------- */
    function switchMode(key) {
      mode.value = key;
      if (sandbox) {
        sandbox.setView(key);
        sandbox.buildBuoys(sandboxItems(), colorOf);
      }
      renderAllCharts();
    }

    function pickDevice(code) {
      const item = latest.value.find((d) => d.device_code === code)
        || FALLBACK_SITES.find((s) => s.code === code);
      if (!item) return;
      selected.value = item;
      if (sandbox) sandbox.focusOn({ x: item.world_x ?? item.x ?? 0, z: item.world_z ?? item.z ?? 0 });
    }

    function closeDetail() {
      selected.value = null;
      if (sandbox) sandbox.focusOn(null);
    }

    function resetView() {
      if (sandbox) sandbox.focusOn(null);
      selected.value = null;
    }

    async function handleAlert(alert, status) {
      try {
        await api.handleAlert(alert.id, { status, note: status === 'resolved' ? '大屏一键处置' : '已派发现场核查' });
        alert.status = status;
        activeAlert.value = null;
        toast('告警处置状态已更新', 'ok');
      } catch (err) {
        toast(`处置失败：${err.message}`, 'err');
      }
    }

    /* ---------- 海洋生态智能体 ---------- */
    function toggleAgent() {
      agent.open = !agent.open;
      if (agent.open) {
        if (!agent.model) loadAgentMeta();
        nextTick(scrollAgent);
      }
    }

    async function loadAgentMeta() {
      try {
        const res = await api.agentStatus();
        const meta = res.data || {};
        agent.model = meta.model || '';
        agent.toolCount = meta.tool_count || 0;
        if (!meta.configured) { agent.statusText = '未配置模型'; agent.statusKind = 'err'; }
        else if (!meta.reachable) { agent.statusText = '降级可用'; agent.statusKind = 'busy'; }
      } catch (err) {
        agent.model = '';
      }
    }

    function scrollAgent() {
      const el = agentBody.value;
      if (el) el.scrollTop = el.scrollHeight;
    }

    function focusAgentTarget(target) {
      const code = target && target.device_code;
      if (code) pickDevice(code);
    }

    function applyAgentFocus(list, reply) {
      const focus = (list || []).slice(0, 6);
      reply.focus = focus;
      if (!focus.length) return;
      const primary = focus.slice().sort((a, b) => (b.level || 0) - (a.level || 0))[0];
      if (primary && primary.device_code) setTimeout(() => pickDevice(primary.device_code), 180);
    }

    function handleAgentEvent(raw, reply) {
      const line = String(raw).split('\n').find((l) => l.startsWith('data:'));
      if (!line) return;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return;
      let event = null;
      try { event = JSON.parse(payload); } catch (e) { return; }

      if (event.type === 'status') {
        agent.statusKind = 'busy';
        agent.statusText = event.text || '处理中';
      } else if (event.type === 'tool') {
        agent.statusKind = 'busy';
        agent.statusText = `调用「${event.label}」`;
        reply.steps.push({ label: event.label, summary: '', done: false, ok: null });
      } else if (event.type === 'tool_result') {
        const step = reply.steps.slice().reverse().find((s) => s.label === event.label && !s.done);
        if (step) { step.done = true; step.ok = event.ok; step.summary = event.summary || ''; }
        agent.statusText = '汇总结论';
      } else if (event.type === 'answer') {
        reply.content = event.text || '';
        reply.pending = false;
      } else if (event.type === 'focus') {
        applyAgentFocus(event.focus, reply);
      } else if (event.type === 'done') {
        if (event.model) agent.model = event.model;
        agent.degraded = !!event.degraded;
      } else if (event.type === 'error') {
        reply.content = event.message || '智能体服务异常';
        reply.pending = false;
        agent.statusKind = 'err';
        agent.statusText = '异常';
      }
      scrollAgent();
    }

    async function askAgent(preset) {
      if (agent.busy) return;
      const question = String(typeof preset === 'string' ? preset : agent.input || '').trim();
      if (!question) return;

      agent.open = true;
      agent.input = '';
      agent.busy = true;
      agent.statusKind = 'busy';
      agent.statusText = '正在理解问题';

      const history = agent.messages
        .filter((m) => m.content && (m.role === 'user' || m.role === 'assistant'))
        .slice(-6)
        .map((m) => ({ role: m.role, content: m.content }));
      agent.messages.push({ role: 'user', content: question, steps: [], focus: [], pending: false });
      const reply = reactive({ role: 'assistant', content: '', steps: [], focus: [], pending: true });
      agent.messages.push(reply);
      await nextTick();
      scrollAgent();

      const headers = { 'Content-Type': 'application/json' };
      if (store.token) headers.Authorization = 'Bearer ' + store.token;

      try {
        const resp = await fetch(api.agentStreamPath(), {
          method: 'POST',
          headers,
          body: JSON.stringify({ question, history }),
        });
        if (!resp.ok || !resp.body) throw new Error(`请求失败（${resp.status}）`);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';
          parts.forEach((part) => handleAgentEvent(part, reply));
        }
        if (buffer.trim()) handleAgentEvent(buffer, reply);
      } catch (err) {
        reply.content = `智能体暂时不可用：${err.message}`;
        agent.statusKind = 'err';
        agent.statusText = '异常';
      } finally {
        reply.pending = false;
        agent.busy = false;
        if (agent.statusKind !== 'err') { agent.statusKind = ''; agent.statusText = '待命'; }
        scrollAgent();
      }
    }

    const tickerText = computed(() => {
      const list = alerts.value.slice(0, 8).map((a) => `${fmt.time(a.created_at)} ${a.title} · ${a.content}`);
      if (!list.length) list.push('系统运行正常，暂无未处置告警');
      return list.join('　　◆　　');
    });

    const selectedRisk = computed(() => {
      if (!selected.value) return null;
      return riskPoints.value.find((p) => p.device_code === selected.value.device_code) || null;
    });

    /* ---- 底栏用户卡片：读取登录态，提供返回后台与退出登录 ---- */
    const ROLE_LABELS = { admin: '管理员', technician: '技术员', farmer: '养殖户', viewer: '访客' };
    const userInfo = ref(store.user || null);
    const userInitials = computed(() => {
      const name = (userInfo.value && userInfo.value.display_name) || '';
      return name ? name.trim().slice(0, 1) : '访';
    });
    const userRole = computed(() => {
      if (!userInfo.value) return '访客浏览模式';
      const role = userInfo.value.role || 'viewer';
      return ROLE_LABELS[role] || role;
    });

    async function loadUser() {
      if (!store.token) { userInfo.value = null; return; }
      try {
        const me = await api.me();
        userInfo.value = me;
        store.user = me;
      } catch (err) {
        userInfo.value = null;
      }
    }

    function openConsole() {
      window.open('/console', '_blank', 'noopener');
    }

    function logout() {
      store.clear();
      userInfo.value = null;
      toast('已退出登录', 'ok');
    }

    let sandbox = null;
    let timer = 0;
    let clockTimer = 0;

    onMounted(async () => {
      mountScreenScale(() => {
        if (sandbox) sandbox.resize();
        Object.values(charts).forEach((c) => c.resize());
      });
      const el = document.getElementById('sandbox');
      if (el && window.THREE) {
        sandbox = createSandbox(el);
        sandbox.onClickBuoy(pickDevice);
        window.__sandbox = sandbox;
      }
      clockTimer = setInterval(() => {
        const now = new Date();
        const p = (n) => String(n).padStart(2, '0');
        clock.time = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
        clock.date = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} 星期${'日一二三四五六'[now.getDay()]}`;
        clock.tick += 1;
      }, 1000);

      loadUser();
      await loadOverview();
      bindRealtime();
      timer = setInterval(loadOverview, 60000);
      window.addEventListener('resize', onResize);
    });

    function onResize() {
      if (sandbox) sandbox.resize();
      Object.values(charts).forEach((c) => c.resize());
    }

    watch(mode, () => { renderAllCharts(); });

    onBeforeUnmount(() => {
      clearInterval(timer);
      clearInterval(clockTimer);
      window.removeEventListener('resize', onResize);
      if (realtime) realtime.close();
      Object.values(charts).forEach((c) => c.dispose());
      if (sandbox) sandbox.dispose();
    });

    return {
      loading, mode, overview, latest, alerts, riskPoints, selected, activeAlert,
      realtimeStatus, summary, metrics, riskSummary, mangrove, ecoShare, riskLevel,
      riskProbability, riskPercent, banner, legend, events, tickerText, clock, selectedRisk, flash,
      VIEW_MODES, METRIC_META, METRIC_ORDER,
      fmt, icon, switchMode, pickDevice, closeDetail, resetView, handleAlert,
      trendOf, deviceTypes, userInfo, userInitials, userRole, openConsole, logout,
      AGENT_EXAMPLES, agent, agentBody, toggleAgent, askAgent, focusAgentTarget,
    };
  },
}).mount('#screen');
