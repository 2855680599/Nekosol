/**
 * Nekosol DOCS - Clean Minimalist Application Controller
 * Handles Home / Docs View Switching, SPA Routing, Command Copy Bar, Dynamic TOC, Search Modal
 */

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initCommandCopy();
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
   3. 命令行一键复制微交互 (Command Copy Bar)
   ========================================================================== */
function initCommandCopy() {
  const cmdPill = document.getElementById('hero-cmd');
  if (!cmdPill) return;

  const code = cmdPill.querySelector('.cmd-text');
  const btn = document.getElementById('hero-cmd-btn');
  const feedback = document.getElementById('hero-copy-feedback');
  if (!code || !btn || !feedback) return;

  cmdPill.addEventListener('click', async () => {
    const copied = await copyTextOrSelect(code);
    feedback.textContent = copied ? '已复制安装命令' : '已选中命令，请按 Ctrl+C 或 Command+C 复制';
    btn.setAttribute('aria-label', copied ? '已复制安装命令' : '已选中安装命令，请手动复制');
    btn.classList.toggle('copied', copied);
    setTimeout(() => {
      feedback.textContent = '';
      btn.setAttribute('aria-label', '复制安装命令');
      btn.classList.remove('copied');
    }, 2500);
  });
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
    document.title = 'Nekosol · 持久化数字个体运行时';
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

  document.title = `${doc.title} · Nekosol Docs`;

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
      resultsContainer.innerHTML = '<div style="padding: 24px; text-align: center; color: var(--c-text-3); font-size: 0.88rem;">键入关键词实时搜索 Nekosol 文档...</div>';
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

async function copyTextOrSelect(code) {
  try {
    if (!navigator.clipboard) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText(code.textContent);
    return true;
  } catch {
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return false;
  }
}

async function copyDocumentationText(code, btn) {
  const origin = btn.textContent;
  const copied = await copyTextOrSelect(code);
  btn.textContent = copied ? '已复制!' : '已选中，请手动复制';
  setTimeout(() => btn.textContent = origin, 2500);
}
