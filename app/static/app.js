/* VENDRIX web panel */
(function () {
  'use strict';

  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);
  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  const won = (value) => `${Number(value || 0).toLocaleString('ko-KR')}원`;
  const toAmount = (value) => Number(String(value == null ? '' : value).replace(/[^\d-]/g, '')) || 0;

  const bootEl = document.getElementById('boot');
  let BOOT = {};
  try {
    BOOT = JSON.parse((bootEl && bootEl.textContent) || '{}');
  } catch (error) {
    console.error('boot parse failed', error);
  }

  let toastTimer = null;
  function toast(message, kind) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.className = `toast show ${kind || ''}`.trim();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3400);
  }

  async function api(path, options) {
    const opts = options || {};
    const init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: { 'X-VDX-CSRF': '1' },
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, init);
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (response.status === 401) {
      toast('로그인이 만료되었습니다. 디스코드에서 /웹패널 을 다시 입력해 주세요.', 'error');
      setTimeout(() => { window.location.href = '/'; }, 1200);
      throw new Error('unauthorized');
    }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `요청에 실패했습니다. (${response.status})`);
    }
    return data;
  }

  function openModal(config) {
    const root = $('#modalRoot');
    if (!root) return;
    const dangerClass = config.danger ? 'danger-btn' : 'primary-btn';
    root.innerHTML = `
      <div class="modal-backdrop" data-close="1">
        <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(config.title)}">
          <div class="modal-head">
            <h3>${esc(config.title)}</h3>
            <button type="button" class="icon-btn" data-close="1" aria-label="닫기">✕</button>
          </div>
          <form id="vdxModalForm">
            ${config.body || ''}
            <div class="modal-actions">
              <button type="button" class="ghost-btn" data-close="1">취소</button>
              <button type="submit" class="${dangerClass}">${esc(config.submitLabel || '확인')}</button>
            </div>
          </form>
        </div>
      </div>`;

    const close = () => { root.innerHTML = ''; };
    $$('#modalRoot [data-close]').forEach((el) => {
      el.addEventListener('click', (event) => {
        if (el.classList.contains('modal-backdrop') && event.target !== el) return;
        close();
      });
    });
    const form = $('#vdxModalForm');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (button) button.disabled = true;
      try {
        await config.onSubmit(new FormData(form));
        close();
      } catch (error) {
        toast(error.message || '처리 중 오류가 발생했습니다.', 'error');
        if (button) button.disabled = false;
      }
    });
    const first = form.querySelector('input, textarea, select');
    if (first) setTimeout(() => first.focus(), 40);
  }

  function confirmModal(title, message, action, danger) {
    openModal({
      title,
      body: `<p class="muted">${esc(message)}</p>`,
      submitLabel: '확인',
      danger: danger !== false,
      onSubmit: async () => { await action(); },
    });
  }

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.getAttribute('data-copy') || '');
      toast('클립보드에 복사했습니다.', 'success');
    } catch (error) {
      toast('복사에 실패했습니다. 직접 선택해 주세요.', 'error');
    }
  });

  function setText(selector, value) {
    const el = $(selector);
    if (el) el.textContent = value;
  }

  /* ============================================================ SHOP PAGE */
  let shop = BOOT.payload || null;
  let busy = false;

  function initShop() {
    if (!shop) return;
    const now = $('#nowText');
    if (now) now.textContent = new Date().toLocaleString('ko-KR', { dateStyle: 'medium', timeStyle: 'short' });

    renderShop();
    bindShopActions();
  }

  function renderShop() {
    const payload = shop;
    setText('#guildPill', payload.guild.name || '서버');
    setText('#userPill', `ID ${String(payload.sessionId || payload.user.id).slice(-6)}`);
    setText('#heroGuild', payload.guild.name || '서버');
    setText('#heroLicense', payload.guild.license_valid ? `LICENSE · ${payload.guild.license_text}` : 'LICENSE 만료');
    const plan = payload.plan || {};
    setText('#heroPlan', plan.name ? `PLAN · ${plan.name}` : 'PLAN');
    setText('#noticeText', payload.guild.notice || '공지사항이 등록되지 않았습니다');
    setText('#eventText', payload.guild.event || '진행 중인 이벤트가 없습니다');
    setText('#balanceValue', payload.user.balance_text);
    setText('#spentValue', `누적 사용 ${payload.user.total_spent_text}`);
    setText('#bankName', payload.bank.name);
    setText('#bankAccount', payload.bank.account);
    setText('#bankHolder', payload.bank.holder);
    setText('#productCount', `${payload.products.length}개 상품`);

    const adminLink = $('#adminLink');
    if (adminLink && BOOT.session && BOOT.session.is_admin) adminLink.classList.remove('hidden');

    const copyButton = $('#copyAccount');
    if (copyButton) copyButton.setAttribute('data-copy', payload.bank.account);

    applyPlanLock();
    renderProducts();
    renderPurchases();
    renderPending();
  }

  function applyPlanLock() {
    const payload = shop;
    const plan = payload.plan || {};
    const autoAllowed = Boolean(plan.auto_charge) && Boolean(payload.auto_charge_available);
    const guildOn = Boolean(payload.guild.auto_charge);
    const lock = $('#autoChargeLock');
    const form = $('#depositForm');
    const allowed = autoAllowed && guildOn;
    if (lock) {
      lock.classList.toggle('hidden', allowed);
      const label = $('#autoChargeLockText');
      if (!plan.auto_charge) {
        setText('#autoChargeLockText', `${plan.name || '현재'} 플랜에서는 자동충전을 사용할 수 없습니다. 스탠다드 플랜부터 가능합니다.`);
      } else if (!guildOn) {
        setText('#autoChargeLockText', '서버 관리자가 자동충전을 꺼두었습니다. 관리자에게 문의하세요.');
      }
    }
    if (form) {
      Array.from(form.elements).forEach((element) => {
        element.disabled = !allowed;
      });
    }
  }

  function deliveryAllowed(product) {
    const plan = (shop && shop.plan) || null;
    if (!plan || !Array.isArray(plan.delivery_types)) return true;
    return plan.delivery_types.includes(product.delivery_type);
  }

  function renderProducts() {
    const grid = $('#productGrid');
    if (!grid) return;
    const products = shop.products || [];
    const buyEnabled = shop.guild.buy_enabled;
    grid.innerHTML = products.map((product) => {
      const stock = product.unlimited
        ? '<span class="stock ok">재고 무제한</span>'
        : product.stock > 0
          ? `<span class="stock ok">재고 ${product.stock}</span>`
          : '<span class="stock out">품절</span>';
      const allowed = deliveryAllowed(product);
      const disabled = product.sold_out || !buyEnabled || !allowed;
      return `
        <article class="product-card ${product.sold_out ? 'is-sold-out' : ''}">
          <div class="product-top">
            <strong class="product-name">${esc(product.name)}</strong>
            ${stock}
          </div>
          <p class="product-desc">${esc(product.description || '상품 설명이 없습니다.')}</p>
          <div class="product-foot">
            <span class="product-price">${esc(product.price_text)}</span>
            <button type="button" class="buy-btn" data-buy="${product.id}" ${disabled ? 'disabled' : ''}>
              ${product.sold_out ? '품절' : !allowed ? '플랜 제한' : buyEnabled ? '구매' : '중지'}
            </button>
          </div>
        </article>`;
    }).join('');

    const empty = $('#productEmpty');
    if (empty) empty.classList.toggle('hidden', products.length > 0);
  }

  function renderPurchases() {
    const body = $('#purchaseRows');
    if (!body) return;
    const rows = shop.purchases || [];
    const statusMap = { completed: ['완료', 'chip-green'], gift: ['지급', 'chip-gold'], refunded: ['환불', 'chip-gray'] };
    body.innerHTML = rows.map((purchase) => {
      const status = statusMap[purchase.status] || [purchase.status, 'chip-gray'];
      return `<tr>
        <td><strong>${esc(purchase.name)}</strong><div class="muted small-text">#${purchase.id}</div></td>
        <td>${esc(purchase.quantity)}</td>
        <td>${esc(purchase.total_text)}</td>
        <td><span class="chip ${status[1]}">${esc(status[0])}</span></td>
        <td class="muted">${esc(purchase.created_text)}</td>
      </tr>`;
    }).join('');
    const empty = $('#purchaseEmpty');
    if (empty) empty.classList.toggle('hidden', rows.length > 0);
  }

  function renderPending() {
    const list = $('#pendingList');
    if (!list) return;
    const pending = shop.pending || [];
    if (!pending.length) {
      list.innerHTML = '';
      return;
    }
    list.innerHTML = pending.map((item) => `
      <div class="pending-item">
        <div>
          <strong>${esc(item.amount_text)}</strong>
          <div class="muted small-text">${esc(item.depositor)} · ${esc(item.expires_text)} 까지</div>
        </div>
        <span class="pending-code">${esc(item.code)}</span>
      </div>`).join('');
  }

  function bindShopActions() {
    const grid = $('#productGrid');
    if (grid) {
      grid.addEventListener('click', (event) => {
        const button = event.target.closest('[data-buy]');
        if (button) openBuy(Number(button.getAttribute('data-buy')));
      });
    }

    const depositForm = $('#depositForm');
    if (depositForm) {
      depositForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (busy) return;
        const data = new FormData(depositForm);
        const amount = toAmount(data.get('amount'));
        const depositor = String(data.get('depositor') || '').trim();
        if (amount < 100) return toast('입금 금액은 100원 이상이어야 합니다.', 'error');
        if (depositor.length < 2) return toast('입금자명을 2자 이상 입력해 주세요.', 'error');
        const button = depositForm.querySelector('button[type="submit"]');
        busy = true;
        if (button) button.disabled = true;
        try {
          const result = await api('/api/deposit', { method: 'POST', body: { amount, depositor } });
          depositForm.reset();
          toast('입금 요청이 생성되었습니다. 안내된 계좌로 입금해 주세요.', 'success');
          showDepositGuide(result.pending);
          await refreshShop();
        } catch (error) {
          toast(error.message, 'error');
        } finally {
          busy = false;
          if (button) button.disabled = false;
        }
      });
    }

    $$('[data-action]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = button.getAttribute('data-action');
        if (action === 'products') {
          const target = $('#productsCard');
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (action === 'buy') {
          const first = (shop.products || []).find((product) => !product.sold_out);
          if (!first) return toast('구매 가능한 상품이 없습니다.', 'error');
          if (!shop.guild.buy_enabled) return toast('현재 구매가 비활성화되어 있습니다.', 'error');
          openBuy(first.id);
        } else if (action === 'me') {
          openProfile();
        } else if (action === 'support') {
          if (shop.support_url) window.open(shop.support_url, '_blank', 'noopener');
          else toast('문의는 서버 관리자에게 DM으로 보내 주세요.');
        }
      });
    });

    const support = $('#supportLink');
    if (support) {
      support.addEventListener('click', () => {
        if (shop.support_url) window.open(shop.support_url, '_blank', 'noopener');
        else toast('문의는 서버 관리자에게 DM으로 보내 주세요.');
      });
    }
  }

  async function refreshShop() {
    const data = await api('/api/panel');
    shop = data;
    renderShop();
  }

  function openBuy(productId) {
    const product = (shop.products || []).find((item) => item.id === productId);
    if (!product) return;
    if (product.sold_out) return toast('품절된 상품입니다.', 'error');
    if (!deliveryAllowed(product)) return toast('현재 플랜에서는 이 전달 방식을 사용할 수 없습니다.', 'error');
    if (!shop.guild.buy_enabled) return toast('현재 구매가 비활성화되어 있습니다.', 'error');
    openModal({
      title: '구매하기',
      submitLabel: '구매',
      body: `
        <div class="modal-product">
          <strong>${esc(product.name)}</strong><br>
          <span class="muted">${esc(product.price_text)} · ${product.unlimited ? '재고 무제한' : '재고 ' + product.stock + '개'}</span>
        </div>
        <label>수량
          <input name="quantity" type="number" min="1" max="10" value="1" required>
        </label>
        <p class="muted small-text">현재 잔액: ${esc(shop.user.balance_text)}</p>`,
      onSubmit: async (data) => {
        const quantity = Number(data.get('quantity'));
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > 10) throw new Error('수량은 1~10 사이로 입력해 주세요.');
        const result = await api('/api/purchase', { method: 'POST', body: { product_id: productId, quantity } });
        toast(`${result.product} ${result.quantity}개 구매 완료 · ${result.delivery}`, 'success');
        await refreshShop();
      },
    });
  }

  function showDepositGuide(pending) {
    openModal({
      title: '입금 안내',
      submitLabel: '확인',
      body: `
        <div class="modal-product">
          <strong>${esc(pending.amount_text)}</strong> 입금 요청이 생성되었습니다.
        </div>
        <div class="bank-box">
          <div class="bank-line"><span>은행</span><strong>${esc(shop.bank.name)}</strong></div>
          <div class="bank-line"><span>계좌</span><strong>${esc(shop.bank.account)}</strong></div>
          <div class="bank-line"><span>예금주</span><strong>${esc(shop.bank.holder)}</strong></div>
          <div class="bank-line"><span>입금자명</span><strong>${esc(pending.depositor)}</strong></div>
          <div class="bank-line"><span>메모(선택)</span><strong class="pending-code">${esc(pending.code)}</strong></div>
        </div>
        <p class="muted small-text">입금자명이 요청과 다르면 자동 충전이 되지 않을 수 있습니다. 만료: ${esc(pending.expires_text)}</p>
        <button type="button" class="secondary-btn" data-copy="${esc(shop.bank.account)}">계좌 복사</button>`,
      onSubmit: async () => {},
    });
  }

  function openProfile() {
    const rows = (shop.purchases || []).slice(0, 6).map((purchase) => `
      <div class="bank-line"><span>${esc(purchase.name)} × ${esc(purchase.quantity)}</span><strong>${esc(purchase.total_text)}</strong></div>`).join('');
    openModal({
      title: '유저정보',
      submitLabel: '닫기',
      body: `
        <div class="modal-product">
          <div class="bank-line"><span>디스코드 ID</span><strong>${esc(shop.user.id)}</strong></div>
          <div class="bank-line"><span>보유 잔액</span><strong>${esc(shop.user.balance_text)}</strong></div>
          <div class="bank-line"><span>누적 사용</span><strong>${esc(shop.user.total_spent_text)}</strong></div>
        </div>
        <h4 class="modal-subtitle">최근 구매</h4>
        ${rows || '<p class="muted small-text">구매 내역이 없습니다.</p>'}
        <p class="muted small-text">자동충전은 Pushbullet 알림을 통해 처리됩니다.</p>`,
      onSubmit: async () => {},
    });
  }

  /* =========================================================== ADMIN PAGE */
  let admin = BOOT.payload || null;
  let owner = Boolean(BOOT.session && BOOT.session.is_owner);
  let adminTab = 'dashboard';

  function initAdmin() {
    if (!admin) return;
    setText('#adminGuild', admin.guild.name || admin.guild.id);
    if (owner) {
      const tab = $('#licenseTab');
      if (tab) tab.classList.remove('hidden');
    }
    renderAdmin();
    bindAdminActions();
    const tabs = ['dashboard', 'products', 'deposits', 'users', 'settings', 'licenses'];
    const hash = (window.location.hash || '').replace('#', '');
    setTab(tabs.includes(hash) ? hash : 'dashboard');
    window.addEventListener('hashchange', () => {
      const next = (window.location.hash || '').replace('#', '');
      if (tabs.includes(next)) setTab(next);
    });
  }

  function renderAdmin() {
    renderAdminHeader();
    renderStats();
    renderAudit();
    renderProductsAdmin();
    renderDeposits();
    renderUsers();
    renderSettings();
    if (owner) loadLicenses();
  }

  function renderAdminHeader() {
    const guild = admin.guild;
    setText('#adminGuild', guild.name || guild.id);
    const label = guild.license_valid ? `LICENSE · ${guild.license_text}` : 'LICENSE 만료';
    const chip = $('#adminLicense');
    if (chip) {
      chip.textContent = label;
      chip.className = `chip ${guild.license_valid ? 'chip-green' : 'chip-red'}`;
    }
  }

  function renderStats() {
    const stats = admin.stats;
    const cards = [
      ['오늘 매출', won(stats.today_sales)],
      ['오늘 주문', `${stats.today_orders}건`],
      ['회원', `${stats.members}명`],
      ['대기 입금', `${stats.pending_deposits}건`],
      ['보유 잔액 합계', won(stats.total_balance)],
      ['전체 매출', won(stats.total_sales)],
    ];
    const grid = $('#statsGrid');
    if (grid) {
      grid.innerHTML = cards.map(([label, value]) => `
        <div class="stat-card"><span class="stat-label">${esc(label)}</span><strong class="stat-value">${esc(value)}</strong></div>`).join('');
    }
    const badge = $('#licenseBadge');
    if (badge) {
      badge.textContent = admin.guild.license_valid ? '정상' : '만료';
      badge.className = `chip ${admin.guild.license_valid ? 'chip-green' : 'chip-red'}`;
    }
    setText('#licenseInfo', `만료일 ${admin.guild.license_text} · 등록 키 ${admin.guild.license_key || '없음'}`);
    const plan = admin.plan || {};
    const limits = admin.limits || {};
    setText(
      '#planInfo',
      `플랜 ${plan.name || '-'} · ${plan.tagline || ''} | 상품 ${limits.products_used ?? 0}/${limits.products_text || '-'} · 코드 ${limits.stock_text || '-'} · 자동충전 ${plan.auto_charge ? '가능' : '불가'}`,
    );
    const limitChip = $('#productLimit');
    if (limitChip) limitChip.textContent = `상품 ${limits.products_used ?? 0} / ${limits.products_text || '-'}`;
  }

  function renderAudit() {
    const list = $('#auditList');
    if (!list) return;
    const rows = (admin.audit || []).slice(0, 12);
    if (!rows.length) {
      list.innerHTML = '<li class="empty">활동 기록이 없습니다.</li>';
      return;
    }
    list.innerHTML = rows.map((row) => `
      <li class="audit-item">
        <div><span class="audit-action">${esc(row.action)}</span> ${esc(row.detail || '')}</div>
        <span class="audit-meta">${esc(new Date(row.created_at * 1000).toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }))}</span>
      </li>`).join('');
  }

  function deliveryLabel(type) {
    return type === 'role' ? '역할 지급' : type === 'content' ? '상품 코드' : '수동 전달';
  }

  function renderProductsAdmin() {
    const body = $('#productRows');
    if (!body) return;
    const products = admin.products || [];
    body.innerHTML = products.map((product) => {
      const stock = product.delivery_type === 'content'
        ? `${product.stock}개`
        : product.unlimited ? '무제한' : `${product.stock}개`;
      return `<tr>
        <td><strong>${esc(product.name)}</strong><div class="muted small-text">#${product.id} · ${esc(product.description || '')}</div></td>
        <td>${esc(product.price_text)}</td>
        <td>${esc(deliveryLabel(product.delivery_type))}</td>
        <td>${esc(stock)}</td>
        <td>${product.active ? '<span class="chip chip-green">판매중</span>' : '<span class="chip chip-gray">숨김</span>'}</td>
        <td class="right">
          <button type="button" class="ghost-btn small" data-product-edit="${product.id}">수정</button>
          ${product.delivery_type === 'content' ? `<button type="button" class="ghost-btn small" data-product-stock="${product.id}">재고</button>` : ''}
          <button type="button" class="ghost-btn small" data-product-delete="${product.id}">삭제</button>
        </td>
      </tr>`;
    }).join('');
    const empty = $('#productEmpty');
    if (empty) empty.classList.toggle('hidden', products.length > 0);
  }

  function renderDeposits() {
    const body = $('#depositRows');
    if (!body) return;
    const filter = ($('#depositFilter') && $('#depositFilter').value) || '';
    const rows = (admin.deposits || []).filter((row) => !filter || row.status === filter);
    const statusMap = {
      matched: ['충전완료', 'chip-green'],
      review: ['검토 필요', 'chip-orange'],
      unmatched: ['미매칭', 'chip-red'],
      ignored: ['무시', 'chip-gray'],
      manual: ['수동', 'chip-gold'],
    };
    body.innerHTML = rows.map((row) => {
      const status = statusMap[row.status] || [row.status, 'chip-gray'];
      const actions = row.status === 'review' || row.status === 'unmatched'
        ? `<button type="button" class="ghost-btn small" data-deposit-approve="${row.id}">지급</button>
           <button type="button" class="ghost-btn small" data-deposit-ignore="${row.id}">무시</button>`
        : row.user_id ? `<span class="muted small-text">${esc(row.user_id)}</span>` : '-';
      return `<tr>
        <td class="muted">${esc(row.created_text)}</td>
        <td>${esc(row.depositor || '-')}</td>
        <td><strong>${esc(row.amount_text)}</strong></td>
        <td><span class="chip ${status[1]}">${esc(status[0])}</span></td>
        <td class="muted small-text">${esc(row.application || row.title || '-')}<div>${esc((row.body || '').slice(0, 60))}</div></td>
        <td class="right">${actions}</td>
      </tr>`;
    }).join('');
    const empty = $('#depositEmpty');
    if (empty) empty.classList.toggle('hidden', rows.length > 0);
  }

  function renderUsers() {
    const body = $('#userRows');
    if (!body) return;
    const users = admin.users || [];
    body.innerHTML = users.map((user) => `
      <tr>
        <td class="mono">${esc(user.id)}</td>
        <td><strong>${esc(user.balance_text)}</strong></td>
        <td class="muted">${esc(user.total_spent_text)}</td>
        <td class="right"><button type="button" class="ghost-btn small" data-user-charge="${esc(user.id)}">충전/차감</button></td>
      </tr>`).join('');
    const empty = $('#userEmpty');
    if (empty) empty.classList.toggle('hidden', users.length > 0);
  }

  function renderSettings() {
    const form = $('#settingsForm');
    if (!form) return;
    form.notice.value = admin.guild.notice || '';
    form.event_text.value = admin.guild.event || '';
    form.log_channel_id.value = admin.guild.log_channel_id || '';
    form.admin_role_id.value = admin.guild.admin_role_id || '';
    form.auto_charge.checked = Boolean(admin.guild.auto_charge);
    form.buy_enabled.checked = Boolean(admin.guild.buy_enabled);
  }

  function setTab(name) {
    adminTab = name;
    $$('.tab-btn').forEach((button) => button.classList.toggle('active', button.getAttribute('data-tab') === name));
    $$('.tab-panel').forEach((panel) => panel.classList.toggle('hidden', panel.getAttribute('data-tab') !== name));
    if (window.location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'licenses' && owner) loadLicenses();
  }

  function bindAdminActions() {
    $$('.tab-btn').forEach((button) => {
      button.addEventListener('click', () => setTab(button.getAttribute('data-tab')));
    });

    const addButton = $('#addProductBtn');
    if (addButton) {
      addButton.addEventListener('click', () => {
        const limits = admin.limits || {};
        if (limits.products_max >= 0 && limits.products_used >= limits.products_max) {
          toast(`상품 등록 한도(${limits.products_text})에 도달했습니다. 상위 플랜이 필요합니다.`, 'error');
          return;
        }
        openProductModal(null);
      });
    }

    const productRows = $('#productRows');
    if (productRows) {
      productRows.addEventListener('click', (event) => {
        const edit = event.target.closest('[data-product-edit]');
        const stock = event.target.closest('[data-product-stock]');
        const remove = event.target.closest('[data-product-delete]');
        if (edit) openProductModal(Number(edit.getAttribute('data-product-edit')));
        if (stock) openStockModal(Number(stock.getAttribute('data-product-stock')));
        if (remove) {
          const product = admin.products.find((item) => item.id === Number(remove.getAttribute('data-product-delete')));
          if (!product) return;
          confirmModal('상품 삭제', `"${product.name}" 상품을 삭제할까요? 재고 코드도 함께 삭제됩니다.`, async () => {
            await api(`/api/admin/product/${product.id}`, { method: 'DELETE' });
            toast('상품을 삭제했습니다.', 'success');
            await refreshAdmin();
          });
        }
      });
    }

    const filter = $('#depositFilter');
    if (filter) filter.addEventListener('change', renderDeposits);

    const depositRows = $('#depositRows');
    if (depositRows) {
      depositRows.addEventListener('click', (event) => {
        const approve = event.target.closest('[data-deposit-approve]');
        const ignore = event.target.closest('[data-deposit-ignore]');
        if (approve) openDepositApprove(Number(approve.getAttribute('data-deposit-approve')));
        if (ignore) {
          const id = Number(ignore.getAttribute('data-deposit-ignore'));
          confirmModal('입금 무시', '이 입금 내역을 무시 처리할까요?', async () => {
            await api(`/api/admin/deposit/${id}/ignore`, { method: 'POST', body: {} });
            toast('무시 처리했습니다.', 'success');
            await refreshAdmin();
          });
        }
      });
    }

    const userRows = $('#userRows');
    if (userRows) {
      userRows.addEventListener('click', (event) => {
        const button = event.target.closest('[data-user-charge]');
        if (button) openChargeModal(button.getAttribute('data-user-charge'));
      });
    }

    const searchButton = $('#userSearchBtn');
    const searchInput = $('#userSearch');
    if (searchButton && searchInput) {
      const search = async () => {
        const query = searchInput.value.trim();
        const data = await api(`/api/admin/users?search=${encodeURIComponent(query)}`);
        admin.users = data.users;
        renderUsers();
      };
      searchButton.addEventListener('click', search);
      searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); search().catch((error) => toast(error.message, 'error')); }
      });
    }

    const settingsForm = $('#settingsForm');
    if (settingsForm) {
      settingsForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(settingsForm);
        const body = {
          notice: String(data.get('notice') || ''),
          event_text: String(data.get('event_text') || ''),
          log_channel_id: String(data.get('log_channel_id') || '').trim(),
          admin_role_id: String(data.get('admin_role_id') || '').trim(),
          auto_charge: settingsForm.auto_charge.checked,
          buy_enabled: settingsForm.buy_enabled.checked,
        };
        await withBusy(settingsForm.querySelector('button[type="submit"]'), async () => {
          await api('/api/admin/settings', { method: 'POST', body });
          toast('설정을 저장했습니다.', 'success');
          await refreshAdmin();
        });
      });
    }

    const licenseForm = $('#licenseForm');
    if (licenseForm) {
      licenseForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(licenseForm);
        const body = {
          count: Number(data.get('count') || 1),
          days: Number(data.get('days') || 30),
          plan: String(data.get('plan') || 'standard'),
          note: String(data.get('note') || ''),
        };
        await withBusy(licenseForm.querySelector('button[type="submit"]'), async () => {
          const result = await api('/api/owner/licenses', { method: 'POST', body });
          const container = $('#licenseResult');
          if (container) {
            container.innerHTML = result.keys.map((key) => `
              <div class="key-row"><span>${esc(key)}</span><button type="button" data-copy="${esc(key)}">복사</button></div>`).join('');
          }
          toast(`라이센스 ${result.keys.length}개 발급 · ${result.plan_name || result.plan}`, 'success');
          await loadLicenses();
        });
      });
    }

    const refreshButton = $('#licenseRefresh');
    if (refreshButton) refreshButton.addEventListener('click', () => loadLicenses().catch((error) => toast(error.message, 'error')));
  }

  async function withBusy(button, action) {
    if (button) button.disabled = true;
    try {
      await action();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function refreshAdmin() {
    const data = await api('/api/admin');
    admin = data;
    renderAdminHeader();
    renderStats();
    renderAudit();
    renderProductsAdmin();
    renderDeposits();
    renderUsers();
    renderSettings();
  }

  function deliveryOptions(current) {
    const labels = { manual: '수동 전달', content: '상품 코드', role: '역할 지급' };
    const plan = (admin && admin.plan) || null;
    const allowed = plan && Array.isArray(plan.delivery_types) ? plan.delivery_types : Object.keys(labels);
    return Object.keys(labels)
      .map((key) => {
        const disabled = allowed.includes(key) ? '' : ' disabled';
        const selected = current === key && !disabled ? ' selected' : '';
        const suffix = disabled ? ' (플랜 업그레이드 필요)' : '';
        return `<option value="${key}"${selected}${disabled}>${labels[key]}${suffix}</option>`;
      })
      .join('');
  }

  function openProductModal(product) {
    const editing = Boolean(product);
    const current = product || { name: '', price: 0, description: '', delivery_type: 'manual', role_id: '', stock: -1, sort_order: 0, active: true };
    openModal({
      title: editing ? '상품 수정' : '상품 추가',
      submitLabel: editing ? '저장' : '추가',
      body: `
        <label>상품명<input name="name" maxlength="80" value="${esc(current.name)}" required></label>
        <div class="form-grid">
          <label>가격(원)<input name="price" type="number" min="0" max="100000000" value="${esc(current.price)}" required></label>
          <label>정렬<input name="sort_order" type="number" min="0" max="1000" value="${esc(current.sort_order || 0)}"></label>
        </div>
        <label>설명<textarea name="description" maxlength="200">${esc(current.description || '')}</textarea></label>
        <div class="form-grid">
          <label>전달 방식
            <select name="delivery_type">
              ${deliveryOptions(current.delivery_type)}
            </select>
          </label>
          <label>역할 ID<input name="role_id" inputmode="numeric" value="${esc(current.role_id || '')}" placeholder="role 전용"></label>
          <label>재고<input name="stock" type="number" min="-1" value="${esc(current.stock == null ? -1 : current.stock)}"></label>
        </div>
        <label class="switch"><input type="checkbox" name="active" ${current.active ? 'checked' : ''}><span class="track"></span><span>판매중</span></label>
        <p class="muted small-text">상품 코드형은 저장 후 [재고] 버튼에서 코드를 등록하세요. 재고 -1은 무제한입니다.</p>`,
      onSubmit: async (data) => {
        const body = {
          name: String(data.get('name') || '').trim(),
          price: Number(data.get('price') || 0),
          description: String(data.get('description') || ''),
          delivery_type: String(data.get('delivery_type') || 'manual'),
          role_id: String(data.get('role_id') || '').trim(),
          stock: Number(data.get('stock') || -1),
          sort_order: Number(data.get('sort_order') || 0),
          active: data.get('active') === 'on',
        };
        if (!body.name) throw new Error('상품명을 입력해 주세요.');
        if (editing) await api(`/api/admin/product/${current.id}`, { method: 'PATCH', body });
        else await api('/api/admin/product', { method: 'POST', body });
        toast(editing ? '상품을 수정했습니다.' : '상품을 추가했습니다.', 'success');
        await refreshAdmin();
      },
    });
  }

  function openStockModal(productId) {
    const product = admin.products.find((item) => item.id === productId);
    if (!product) return;
    openModal({
      title: '상품 코드 등록',
      submitLabel: '등록',
      body: `
        <div class="modal-product"><strong>${esc(product.name)}</strong><br><span class="muted">현재 재고 ${product.stock}개</span></div>
        <label>상품 코드 (한 줄에 하나)
          <textarea name="lines" rows="8" placeholder="CODE-0001&#10;CODE-0002" required></textarea>
        </label>
        <p class="muted small-text">등록된 코드는 구매 시 1개씩 차감되어 구매자 DM으로 전송됩니다.</p>`,
      onSubmit: async (data) => {
        const lines = String(data.get('lines') || '');
        const result = await api(`/api/admin/product/${productId}/stock`, { method: 'POST', body: { lines } });
        toast(`${result.added}개 등록 · 현재 재고 ${result.stock}개`, 'success');
        await refreshAdmin();
      },
    });
  }

  function openDepositApprove(depositId) {
    const deposit = (admin.deposits || []).find((row) => row.id === depositId);
    if (!deposit) return;
    openModal({
      title: '입금 수동 지급',
      submitLabel: '지급',
      body: `
        <div class="modal-product">
          <strong>${esc(deposit.amount_text)}</strong><br>
          <span class="muted">${esc(deposit.depositor || '입금자 미상')} · ${esc(deposit.created_text)}</span>
        </div>
        <label>지급할 회원 ID
          <input name="user_id" inputmode="numeric" value="${esc(deposit.user_id || '')}" placeholder="디스코드 유저 ID" required>
        </label>
        <p class="muted small-text">입금 금액이 회원 잔액에 충전되고, 회원에게 DM 알림이 발송됩니다.</p>`,
      onSubmit: async (data) => {
        const userId = String(data.get('user_id') || '').trim();
        if (!/^\d+$/.test(userId)) throw new Error('디스코드 유저 ID를 숫자로 입력해 주세요.');
        await api(`/api/admin/deposit/${depositId}/approve`, { method: 'POST', body: { user_id: userId } });
        toast('입금을 승인하고 잔액을 충전했습니다.', 'success');
        await refreshAdmin();
      },
    });
  }

  function openChargeModal(userId) {
    openModal({
      title: '충전 / 차감',
      submitLabel: '처리',
      body: `
        <label>대상 회원 ID<input name="user_id" inputmode="numeric" value="${esc(userId || '')}" required></label>
        <label>금액(원)<input name="amount" type="number" placeholder="10000 / 차감은 -10000" required></label>
        <p class="muted small-text">음수 금액은 잔액에서 차감됩니다. 잔액보다 많이 차감할 수 없습니다.</p>`,
      onSubmit: async (data) => {
        const id = String(data.get('user_id') || '').trim();
        const amount = Number(data.get('amount') || 0);
        if (!/^\d+$/.test(id)) throw new Error('유저 ID를 숫자로 입력해 주세요.');
        if (!amount) throw new Error('0원은 처리할 수 없습니다.');
        const result = await api('/api/admin/charge', { method: 'POST', body: { user_id: id, amount } });
        toast(`처리 완료 · 현재 잔액 ${result.balance_text}`, 'success');
        await refreshAdmin();
      },
    });
  }

  async function loadLicenses() {
    if (!owner) return;
    const [summary, licenses] = await Promise.all([
      api('/api/owner/summary'),
      api('/api/owner/licenses'),
    ]);
    const stats = summary.summary;
    const grid = $('#licenseStats');
    if (grid) {
      const cards = [
        ['전체 발급', `${stats.total}개`],
        ['사용됨', `${stats.used}개`],
        ['활성 서버', `${stats.active_guilds}개`],
        ['폐기', `${stats.revoked}개`],
      ];
      grid.innerHTML = cards.map(([label, value]) => `
        <div class="stat-card"><span class="stat-label">${esc(label)}</span><strong class="stat-value">${esc(value)}</strong></div>`).join('');
    }
    const body = $('#licenseRows');
    if (body) {
      const rows = licenses.licenses || [];
      const statusClass = { '미사용': 'chip-gray', '사용': 'chip-green', '폐기': 'chip-red' };
      body.innerHTML = rows.map((row) => `
        <tr>
          <td class="mono">${esc(row.key)}</td>
          <td>${esc(row.plan_name || row.plan)}</td>
          <td>${esc(row.duration_days)}일</td>
          <td><span class="chip ${statusClass[row.status] || 'chip-gray'}">${esc(row.status)}</span></td>
          <td class="mono">${esc(row.redeemed_by || '-')}</td>
          <td class="muted">${esc(row.created_at)}</td>
          <td class="right">${row.status === '폐기' ? '-' : `<button type="button" class="ghost-btn small" data-license-revoke="${esc(row.key)}">폐기</button>`}</td>
        </tr>`).join('');
      const empty = $('#licenseEmpty');
      if (empty) empty.classList.toggle('hidden', rows.length > 0);

      if (!body.dataset.bound) {
        body.dataset.bound = '1';
        body.addEventListener('click', (event) => {
          const button = event.target.closest('[data-license-revoke]');
          if (!button) return;
          const key = button.getAttribute('data-license-revoke');
          confirmModal('라이센스 폐기', `${key} 키를 폐기할까요? 사용 중인 서버라면 라이센스도 만료됩니다.`, async () => {
            await api('/api/owner/licenses/revoke', { method: 'POST', body: { key } });
            toast('라이센스를 폐기했습니다.', 'success');
            await loadLicenses();
            await refreshAdmin();
          });
        });
      }
    }
  }

  const page = document.body.getAttribute('data-page');
  if (page === 'shop') initShop();
  if (page === 'admin') initAdmin();
})();
