/**
 * CHIYO DOCS - Clean Minimalist Application Controller
 * Handles Home / Docs View Switching, SPA Routing, Spotlight Cards, Command Copy Bar, Dynamic TOC, Search Modal
 */

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initSpotlightCards();
  initCommandCopy();
  initChronoCore();
  renderSidebar();
  initRouting();
  initSearch();
  initMobileMenu();
});

/* ==========================================================================
   1. 主题切换 (Theme Toggle)
   ========================================================================== */
function initTheme() {
  const themeBtn = document.getElementById('theme-toggle');
  if (!themeBtn) return;

  let savedTheme = 'dark'; try { savedTheme = localStorage.getItem('chiyo-theme') || 'dark'; } catch {}
  document.documentElement.setAttribute('data-theme', savedTheme);
  updateThemeIcon(savedTheme);

  themeBtn.addEventListener('click', () => {
    const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
    const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    try { localStorage.setItem('chiyo-theme', nextTheme); } catch {}
    updateThemeIcon(nextTheme);
  });
}

function updateThemeIcon(theme) {
  const iconEl = document.getElementById('theme-icon');
  const btnEl = document.getElementById('theme-toggle');
  if (!iconEl || !btnEl) return;

  if (theme === 'dark') {
    iconEl.innerHTML = `
      <circle cx="12" cy="12" r="5"></circle>
      <line x1="12" y1="1" x2="12" y2="3"></line>
      <line x1="12" y1="21" x2="12" y2="23"></line>
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
      <line x1="1" y1="12" x2="3" y2="12"></line>
      <line x1="21" y1="12" x2="23" y2="12"></line>
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
    `;
    btnEl.setAttribute('title', '切换为亮色模式');
  } else {
    iconEl.innerHTML = `
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
    `;
    btnEl.setAttribute('title', '切换为暗色模式');
  }
}

/* ==========================================================================
   2. 卡片聚光灯微动效 (Spotlight Card Hover Tracking)
   ========================================================================== */
function initSpotlightCards() {
  const cards = document.querySelectorAll('.spotlight-card');
  cards.forEach(card => {
    card.addEventListener('mousemove', e => {
      const rect = card.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      card.style.setProperty('--mouse-x', `${x}px`);
      card.style.setProperty('--mouse-y', `${y}px`);
    });
  });
}

/* ==========================================================================
   3. 命令行一键复制微交互 (Command Copy Bar)
   ========================================================================== */
function initCommandCopy() {
  const cmdPill = document.getElementById('hero-cmd');
  if (!cmdPill) return;

  const defaultIcon = cmdPill.querySelector('.copy-icon-default');
  const successIcon = cmdPill.querySelector('.copy-icon-success');
  const btn = document.getElementById('hero-cmd-btn');

  cmdPill.addEventListener('click', () => {
    const code = cmdPill.querySelector('.cmd-text')?.textContent || 'bash scripts/install.sh';
    navigator.clipboard.writeText(code).then(() => {
      if (defaultIcon && successIcon) {
        defaultIcon.style.display = 'none';
        successIcon.style.display = 'inline-block';
        if (btn) btn.classList.add('copied');

        setTimeout(() => {
          defaultIcon.style.display = 'inline-block';
          successIcon.style.display = 'none';
          if (btn) btn.classList.remove('copied');
        }, 1600);
      }
    });
  });
}

/* ==========================================================================
   4. 千代时空晶核 (Chrono-Core 3D Engine)
   ========================================================================== */
function initChronoCore() {
  const canvas = document.getElementById('chrono-canvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const wrap = canvas.parentElement;
  let width = (canvas.width = wrap.clientWidth || 600);
  let height = (canvas.height = wrap.clientHeight || 260);

  window.addEventListener('resize', () => {
    if (wrap.clientWidth > 0) {
      width = canvas.width = wrap.clientWidth;
      height = canvas.height = wrap.clientHeight;
    }
  });

  // 3D 几何正十二面体 / 拓扑晶核点位计算
  const phi = (1 + Math.sqrt(5)) / 2;
  const baseVertices = [
    [-1, -1, -1], [-1, -1, 1], [-1, 1, -1], [-1, 1, 1],
    [1, -1, -1], [1, -1, 1], [1, 1, -1], [1, 1, 1],
    [0, -1 / phi, -phi], [0, -1 / phi, phi], [0, 1 / phi, -phi], [0, 1 / phi, phi],
    [-1 / phi, -phi, 0], [-1 / phi, phi, 0], [1 / phi, -phi, 0], [1 / phi, phi, 0],
    [-phi, 0, -1 / phi], [phi, 0, -1 / phi], [-phi, 0, 1 / phi], [phi, 0, 1 / phi]
  ];

  // 提取线框连接 (近距离点对连接)
  const edges = [];
  for (let i = 0; i < baseVertices.length; i++) {
    for (let j = i + 1; j < baseVertices.length; j++) {
      const v1 = baseVertices[i];
      const v2 = baseVertices[j];
      const d = Math.hypot(v1[0] - v2[0], v1[1] - v2[1], v1[2] - v2[2]);
      if (Math.abs(d - 2 / phi) < 0.05) {
        edges.push([i, j]);
      }
    }
  }

  let rotX = 0.3;
  let rotY = 0;
  let targetRotX = 0.3;
  let targetRotY = 0;
  let isHovered = false;
  let pulseRadius = 0;
  let pulseMax = 0;

  wrap.addEventListener('mousemove', e => {
    const rect = wrap.getBoundingClientRect();
    const nx = (e.clientX - rect.left) / rect.width - 0.5;
    const ny = (e.clientY - rect.top) / rect.height - 0.5;
    targetRotY = nx * 1.8;
    targetRotX = -ny * 1.2;
    isHovered = true;
  });

  wrap.addEventListener('mouseleave', () => {
    isHovered = false;
  });

  wrap.addEventListener('click', () => {
    pulseRadius = 10;
    pulseMax = Math.max(width, height) * 0.75;
  });

  function project(p, size) {
    const cosY = Math.cos(rotY);
    const sinY = Math.sin(rotY);
    const cosX = Math.cos(rotX);
    const sinX = Math.sin(rotX);

    // 绕 Y 轴
    let x1 = p[0] * cosY - p[2] * sinY;
    let z1 = p[0] * sinY + p[2] * cosY;

    // 绕 X 轴
    let y2 = p[1] * cosX - z1 * sinX;
    let z2 = p[1] * sinX + z1 * cosX;

    const fov = 340;
    const scale = fov / (fov + z2 * 45);

    return {
      x: width / 2 + x1 * size * scale,
      y: height / 2 + y2 * size * scale,
      z: z2,
      scale
    };
  }

  let autoTime = 0;

  function render() {
    ctx.clearRect(0, 0, width, height);

    autoTime += 0.012;
    if (!isHovered) {
      targetRotY += 0.007;
      targetRotX = Math.sin(autoTime * 0.5) * 0.25;
    }
    rotX += (targetRotX - rotX) * 0.06;
    rotY += (targetRotY - rotY) * 0.06;

    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const accentColor = isLight ? '2, 132, 199' : '56, 189, 248';
    const dimColor = isLight ? '15, 23, 42' : '226, 232, 240';

    // 1. 绘制生命活动同心能量光环 (Orbital Rings)
    const ringRadii = [90, 130, 168];
    const ringTilts = [0.25, -0.4, 0.65];
    const ringSpeeds = [0.015, -0.01, 0.008];

    for (let r = 0; r < ringRadii.length; r++) {
      ctx.save();
      ctx.translate(width / 2, height / 2);
      ctx.rotate(rotY * 0.3 + ringTilts[r]);
      ctx.scale(1, 0.32);

      ctx.beginPath();
      ctx.arc(0, 0, ringRadii[r], 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(${accentColor}, ${0.12 - r * 0.02})`;
      ctx.lineWidth = 1;
      ctx.stroke();

      // 轨道上的微型运行卫星节点 (Typed Node)
      const satAngle = autoTime * (ringSpeeds[r] * 60);
      const sx = Math.cos(satAngle) * ringRadii[r];
      const sy = Math.sin(satAngle) * ringRadii[r];
      ctx.beginPath();
      ctx.arc(sx, sy, 3, 0, Math.PI * 2);
      ctx.fillStyle = `rgb(${accentColor})`;
      ctx.shadowColor = `rgb(${accentColor})`;
      ctx.shadowBlur = 8;
      ctx.fill();
      ctx.shadowBlur = 0;

      ctx.restore();
    }

    // 2. 点击爆发心跳扩散环 (Pulse Wave)
    if (pulseRadius > 0 && pulseRadius < pulseMax) {
      pulseRadius += (pulseMax - pulseRadius) * 0.08 + 1.2;
      const alpha = Math.max(0, 1 - pulseRadius / pulseMax) * 0.45;
      ctx.beginPath();
      ctx.arc(width / 2, height / 2, pulseRadius, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(${accentColor}, ${alpha})`;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // 3. 核心 3D 拓扑晶核多面体绘制
    const projected = baseVertices.map(v => project(v, 48));

    // 绘制晶核外连线
    ctx.lineWidth = 1.2;
    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      const p1 = projected[e[0]];
      const p2 = projected[e[1]];
      const avgZ = (p1.z + p2.z) / 2;
      const alpha = Math.min(0.65, Math.max(0.15, (avgZ + 2.5) / 5));

      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.strokeStyle = `rgba(${accentColor}, ${alpha})`;
      ctx.stroke();
    }

    // 绘制晶体节点端点
    for (let i = 0; i < projected.length; i++) {
      const p = projected[i];
      const alpha = Math.min(0.9, Math.max(0.2, (p.z + 2.5) / 5));

      ctx.beginPath();
      ctx.arc(p.x, p.y, 2.2 * p.scale, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${dimColor}, ${alpha})`;
      ctx.fill();
    }

    // 晶核中心发光微核 (Heartbeat Core)
    const coreGlow = Math.sin(autoTime * 4) * 2 + 6;
    ctx.beginPath();
    ctx.arc(width / 2, height / 2, coreGlow, 0, Math.PI * 2);
    ctx.fillStyle = `rgb(${accentColor})`;
    ctx.shadowColor = `rgb(${accentColor})`;
    ctx.shadowBlur = 16;
    ctx.fill();
    ctx.shadowBlur = 0;

    requestAnimationFrame(render);
  }

  requestAnimationFrame(render);
}

/* ==========================================================================
   4. 渲染左侧分类目录 (Sidebar Render)
   ========================================================================== */
function renderSidebar() {
  const sidebarEl = document.getElementById('docs-sidebar');
  if (!sidebarEl || typeof DOCS_TREE === 'undefined') return;

  let html = '';
  DOCS_TREE.forEach(group => {
    html += `
      <div class="sidebar-group">
        <div class="sidebar-title">${escapeHtml(group.category)}</div>
        <ul class="sidebar-list">
    `;
    group.items.forEach(item => {
      html += `
        <li class="sidebar-item">
          <a href="#${item.id}" class="sidebar-link" data-id="${item.id}">
            <span>${escapeHtml(item.title)}</span>
            ${item.badge ? `<span class="sidebar-badge">${escapeHtml(item.badge)}</span>` : ''}
          </a>
        </li>
      `;
    });
    html += `
        </ul>
      </div>
    `;
  });

  sidebarEl.innerHTML = html;
}

/* ==========================================================================
   5. 双视图路由切换 (Home View vs Docs View)
   ========================================================================== */
function initRouting() {
  window.addEventListener('hashchange', () => {
    routeHash();
  });

  routeHash();
}

function routeHash() {
  const rawHash = location.hash.replace('#', '').trim();
  const homeView = document.getElementById('view-home');
  const docsView = document.getElementById('view-docs');
  const navHome = document.getElementById('nav-home');
  const navDocs = document.getElementById('nav-docs');
  const navQuickstart = document.getElementById('nav-quickstart');
  const navStatus = document.getElementById('nav-status');

  [navHome, navDocs, navQuickstart, navStatus].forEach(nav => {
    if (nav) nav.classList.remove('active');
  });

  if (!rawHash || rawHash === 'home') {
    if (homeView) homeView.style.display = 'flex';
    if (docsView) docsView.style.display = 'none';
    if (navHome) navHome.classList.add('active');
    document.title = 'CHIYO · 持久化数字个体运行时';
    window.scrollTo({ top: 0, behavior: 'instant' });
    return;
  }

  if (homeView) homeView.style.display = 'none';
  if (docsView) docsView.style.display = 'flex';

  if (rawHash === 'quickstart' && navQuickstart) {
    navQuickstart.classList.add('active');
  } else if ((rawHash === 'status-matrix' || rawHash === 'current-status') && navStatus) {
    navStatus.classList.add('active');
  } else if (navDocs) {
    navDocs.classList.add('active');
  }

  loadDoc(rawHash);
}

function loadDoc(docId) {
  const doc = DOCS_CONTENT[docId] || DOCS_CONTENT['intro'];
  const targetId = DOCS_CONTENT[docId] ? docId : 'intro';

  document.title = `${doc.title} · CHIYO Docs`;

  document.querySelectorAll('.sidebar-link').forEach(link => {
    if (link.getAttribute('data-id') === targetId) {
      link.classList.add('active');
    } else {
      link.classList.remove('active');
    }
  });

  let categoryName = '文档';
  if (typeof DOCS_TREE !== 'undefined') {
    for (const group of DOCS_TREE) {
      const found = group.items.find(i => i.id === targetId);
      if (found) {
        categoryName = group.category;
        break;
      }
    }
  }

  const container = document.getElementById('doc-container');
  if (container) {
    container.innerHTML = `
      <div class="breadcrumbs">
        <a href="#home">首页</a>
        <span class="sep">/</span>
        <a href="#intro">文档</a>
        <span class="sep">/</span>
        <span>${escapeHtml(categoryName)}</span>
        <span class="sep">/</span>
        <span style="color: var(--c-text-1); font-weight: 500;">${escapeHtml(doc.title)}</span>
      </div>

      <h1 class="page-title">${escapeHtml(doc.title)}</h1>
      ${doc.summary ? `<p class="page-summary">${escapeHtml(doc.summary)}</p>` : ''}

      <div class="markdown-body">
        ${doc.content}
      </div>

      <div class="page-pager" id="page-pager">
        ${renderPager(targetId)}
      </div>
    `;
  }

  renderTOC(doc.toc || []);
  bindCopyButtons();

  const sidebarEl = document.getElementById('docs-sidebar');
  if (sidebarEl) sidebarEl.classList.remove('open');

  window.scrollTo({ top: 0, behavior: 'instant' });
}

function renderPager(currentId) {
  const flatItems = [];
  DOCS_TREE.forEach(group => {
    group.items.forEach(item => flatItems.push(item));
  });

  const currentIndex = flatItems.findIndex(i => i.id === currentId);
  const prev = currentIndex > 0 ? flatItems[currentIndex - 1] : null;
  const next = currentIndex < flatItems.length - 1 ? flatItems[currentIndex + 1] : null;

  let html = '';
  if (prev) {
    html += `
      <a href="#${prev.id}" class="pager-btn pager-prev">
        <span class="pager-dir">上一篇</span>
        <span class="pager-title">← ${escapeHtml(prev.title)}</span>
      </a>
    `;
  } else {
    html += `<div></div>`;
  }

  if (next) {
    html += `
      <a href="#${next.id}" class="pager-btn pager-next">
        <span class="pager-dir">下一篇</span>
        <span class="pager-title">${escapeHtml(next.title)} →</span>
      </a>
    `;
  } else {
    html += `<div></div>`;
  }

  return html;
}

/* ==========================================================================
   6. 右侧本页目录与滚动 (TOC)
   ========================================================================== */
function renderTOC(tocList) {
  const tocListEl = document.getElementById('toc-list');
  if (!tocListEl) return;

  if (!tocList || tocList.length === 0) {
    tocListEl.innerHTML = '<li class="toc-item" style="color: var(--c-text-3); font-size: 0.8rem;">本页暂无子目录</li>';
    return;
  }

  let html = '';
  tocList.forEach(item => {
    html += `
      <li class="toc-item">
        <a href="#${item.id}" class="toc-link" data-anchor="${item.id}">${escapeHtml(item.text)}</a>
      </li>
    `;
  });

  tocListEl.innerHTML = html;

  tocListEl.querySelectorAll('.toc-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const anchorId = link.getAttribute('data-anchor');
      const targetEl = document.getElementById(anchorId);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: 'smooth' });
        history.replaceState(null, '', `#${docAnchorPage(anchorId)}`);
      }
    });
  });
}

function bindCopyButtons() {
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const codeBlock = btn.closest('.code-block');
      if (!codeBlock) return;
      const code = codeBlock.querySelector('code');
      if (!code) return;

      copyDocumentationText(code, btn);
    });
  });
}

/* ==========================================================================
   7. 全局搜索 (Ctrl+K Search Modal)
   ========================================================================== */
function initSearch() {
  const searchBtn = document.getElementById('search-trigger');
  const modalBackdrop = document.getElementById('search-modal');
  const searchInput = document.getElementById('search-input');
  const resultsContainer = document.getElementById('search-results');

  if (!searchBtn || !modalBackdrop || !searchInput || !resultsContainer) return;

  const openSearch = () => {
    modalBackdrop.classList.add('show');
    searchInput.value = '';
    renderSearchResults('');
    setTimeout(() => searchInput.focus(), 50);
  };

  const closeSearch = () => {
    modalBackdrop.classList.remove('show');
  };

  searchBtn.addEventListener('click', openSearch);

  modalBackdrop.addEventListener('click', (e) => {
    if (e.target === modalBackdrop) closeSearch();
  });

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      modalBackdrop.classList.contains('show') ? closeSearch() : openSearch();
    }
    if (e.key === 'Escape' && modalBackdrop.classList.contains('show')) {
      closeSearch();
    }
  });

  searchInput.addEventListener('input', (e) => {
    renderSearchResults(e.target.value.trim().toLowerCase());
  });

  function renderSearchResults(query) {
    if (!query) {
      resultsContainer.innerHTML = '<div style="padding: 24px; text-align: center; color: var(--c-text-3); font-size: 0.88rem;">键入关键词实时搜索千代文档...</div>';
      return;
    }

    const matches = [];
    Object.keys(DOCS_CONTENT).forEach(key => {
      const doc = DOCS_CONTENT[key];
      const titleMatch = doc.title.toLowerCase().includes(query);
      const summaryMatch = (doc.summary || '').toLowerCase().includes(query);
      const contentMatch = doc.content.toLowerCase().includes(query);

      if (titleMatch || summaryMatch || contentMatch) {
        matches.push({
          id: key,
          title: doc.title,
          summary: doc.summary || '点击进入阅读此章节'
        });
      }
    });

    if (matches.length === 0) {
      resultsContainer.innerHTML = `<div style="padding: 24px; text-align: center; color: var(--c-text-3); font-size: 0.88rem;">未找到与 "<strong>${escapeHtml(query)}</strong>" 相关的结果</div>`;
      return;
    }

    let html = '';
    matches.forEach(item => {
      html += `
        <a href="#${item.id}" class="search-result-item" onclick="document.getElementById('search-modal').classList.remove('show')">
          <div class="search-result-title">
            <span style="color: var(--c-accent);">#</span>
            <span>${escapeHtml(item.title)}</span>
          </div>
          <div class="search-result-summary">${escapeHtml(item.summary)}</div>
        </a>
      `;
    });

    resultsContainer.innerHTML = html;
  }
}

/* ==========================================================================
   8. 移动端抽屉 (Mobile Drawer)
   ========================================================================== */
function initMobileMenu() {
  const toggleBtn = document.getElementById('mobile-toggle');
  const sidebarEl = document.getElementById('docs-sidebar');

  if (!toggleBtn || !sidebarEl) return;

  toggleBtn.addEventListener('click', () => {
    sidebarEl.classList.toggle('open');
  });
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function docAnchorPage(anchorId) { return anchorId.split("-section-")[0]; }

async function copyDocumentationText(code, btn) {
  const origin = btn.textContent;
  try {
    if (!navigator.clipboard) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(code.textContent);
    btn.textContent = '已复制!';
  } catch {
    const range = document.createRange(); range.selectNodeContents(code);
    const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    btn.textContent = '已选中，请手动复制';
  }
  setTimeout(() => btn.textContent = origin, 1800);
}
