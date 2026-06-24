/**
 * viewer3d.js  —  Three.js STL viewer (local build, no CDN dependency).
 */

import * as THREE from './three.module.js';
import { OrbitControls } from './OrbitControls.js';
import { STLLoader } from './STLLoader.js';

export class Viewer3D {
  constructor(container) {
    this._container = container;
    this._mesh = null;
    this._init();
  }

  _init() {
    const W = this._container.clientWidth || 800;
    const H = this._container.clientHeight || 600;

    // Renderer
    this._renderer = new THREE.WebGLRenderer({ antialias: true });
    this._renderer.setPixelRatio(window.devicePixelRatio);
    this._renderer.setSize(W, H);
    this._renderer.shadowMap.enabled = true;
    this._container.appendChild(this._renderer.domElement);

    // Scene
    this._scene = new THREE.Scene();
    this._scene.background = new THREE.Color(0x0d1018);

    // Grid
    const grid = new THREE.GridHelper(200, 20, 0x1e2236, 0x1e2236);
    this._scene.add(grid);

    // Lights — tuned for silver metallic material
    this._scene.add(new THREE.AmbientLight(0xffffff, 0.35));
    const dir1 = new THREE.DirectionalLight(0xffffff, 1.1);
    dir1.position.set(100, 150, 100);
    dir1.castShadow = true;
    this._scene.add(dir1);
    const dir2 = new THREE.DirectionalLight(0xd0e8ff, 0.6);
    dir2.position.set(-80, 60, -80);
    this._scene.add(dir2);
    const dir3 = new THREE.DirectionalLight(0xfff0e0, 0.4);
    dir3.position.set(50, -60, 80);
    this._scene.add(dir3);
    const fill = new THREE.DirectionalLight(0xffffff, 0.25);
    fill.position.set(0, -100, 0);
    this._scene.add(fill);

    // Camera
    this._camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 10000);
    this._camera.position.set(60, 60, 60);

    // Orbit controls
    this._controls = new OrbitControls(this._camera, this._renderer.domElement);
    this._controls.enableDamping = true;
    this._controls.dampingFactor = 0.08;
    this._controls.minDistance = 1;
    this._controls.maxDistance = 5000;

    // Resize observer
    this._ro = new ResizeObserver(() => this._onResize());
    this._ro.observe(this._container);

    this._animate();
  }

  _animate() {
    requestAnimationFrame(() => this._animate());
    this._controls.update();
    this._renderer.render(this._scene, this._camera);
  }

  _onResize() {
    const W = this._container.clientWidth;
    const H = this._container.clientHeight;
    if (!W || !H) return;
    this._camera.aspect = W / H;
    this._camera.updateProjectionMatrix();
    this._renderer.setSize(W, H);
  }

  loadSTL(url) {
    this._setStatus('loading');
    const loader = new STLLoader();
    loader.load(
      url,
      (geometry) => {
        if (this._mesh) {
          this._scene.remove(this._mesh);
          this._mesh.geometry.dispose();
          this._mesh.material.dispose();
          this._mesh = null;
        }
        geometry.computeVertexNormals();
        const mat = new THREE.MeshPhongMaterial({
          color:    0xc8ccd4,
          specular: 0xffffff,
          shininess: 320,
          emissive:  0x0a0c10,
          side: THREE.DoubleSide,
        });
        this._mesh = new THREE.Mesh(geometry, mat);
        this._mesh.castShadow = true;
        this._mesh.receiveShadow = true;
        this._scene.add(this._mesh);
        this._fitCamera();
        this._setStatus('hidden');
      },
      undefined,
      (err) => {
        console.error('STL load error:', err);
        this._setStatus('error', url);
      }
    );
  }

  _fitCamera() {
    if (!this._mesh) return;
    const box = new THREE.Box3().setFromObject(this._mesh);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 10;
    const dist = maxDim * 2.5;
    this._controls.target.copy(center);
    this._camera.position.set(
      center.x + dist * 0.7,
      center.y + dist * 0.7,
      center.z + dist * 0.7
    );
    this._camera.near = maxDim * 0.001;
    this._camera.far = maxDim * 200;
    this._camera.updateProjectionMatrix();
    this._controls.update();
  }

  hasModel() { return this._mesh !== null; }

  resetView() { this._fitCamera(); }

  clearModel() {
    if (this._mesh) {
      this._scene.remove(this._mesh);
      this._mesh.geometry.dispose();
      this._mesh.material.dispose();
      this._mesh = null;
    }
    this._setStatus('visible');
  }

  _setStatus(state, url) {
    const ph = document.getElementById('viewer-placeholder');
    if (!ph) return;
    const overlay = ph.querySelector('.viewer-status-overlay');
    if (overlay) overlay.remove();

    if (state === 'hidden') {
      ph.classList.add('hidden');
      return;
    }
    ph.classList.remove('hidden');
    if (state === 'loading') {
      const el = document.createElement('div');
      el.className = 'viewer-status-overlay';
      el.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:rgba(13,16,24,.85);pointer-events:none;';
      el.innerHTML = '<div style="font-size:36px;animation:pulse 1.2s infinite">⧖</div><div>加载模型中…</div>';
      ph.appendChild(el);
    } else if (state === 'error') {
      const el = document.createElement('div');
      el.className = 'viewer-status-overlay';
      el.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;background:rgba(13,16,24,.85);pointer-events:none;';
      el.innerHTML = `<div style="font-size:36px;opacity:.5">⚠</div><div style="color:#e05252">模型加载失败</div><div style="font-size:11px;opacity:.6;max-width:80%;text-align:center;word-break:break-all;">${url || ''}</div>`;
      ph.appendChild(el);
    }
    // state === 'visible' → just show the original placeholder (done above)
  }
}
