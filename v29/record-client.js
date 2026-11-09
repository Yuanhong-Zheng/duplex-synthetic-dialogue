/* Register record access before the workflow requests any packed source file. */
(() => {
  const script = document.currentScript;
  const base = new URL('./', script.src);
  const workerURL = new URL('record-worker.js', base).href;
  const recordsPath = new URL('records/', base).pathname;
  const worker = navigator.serviceWorker;
  let notice;

  function showNotice(message, error) {
    const render = () => {
      if (!notice) {
        notice = document.createElement('div');
        notice.setAttribute('role', 'status');
        notice.style.cssText = 'position:relative;z-index:1000;padding:12px 16px;background:#fff4d5;color:#533f17;font:14px/1.5 sans-serif';
        document.body.prepend(notice);
      }
      notice.replaceChildren(document.createTextNode(message + ' '));
      if (error) {
        const link = document.createElement('a');
        link.href = new URL('workflow-records.zip', base).href;
        link.textContent = '下载完整流程资料';
        notice.append(link);
      }
    };
    if (document.body) render();
    else document.addEventListener('DOMContentLoaded', render, {once: true});
  }

  function isRecord(url) {
    return url.origin === base.origin && url.pathname.startsWith(recordsPath);
  }
  function controlled() {
    return worker && worker.controller && worker.controller.scriptURL === workerURL;
  }

  const ready = (async () => {
    if (!worker) throw new Error('service worker unavailable');
    await worker.register(workerURL, {scope: base.pathname});
    if (!controlled()) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          worker.removeEventListener('controllerchange', check);
          reject(new Error('record worker activation timed out'));
        }, 30000);
        function check() {
          if (!controlled()) return;
          clearTimeout(timer);
          worker.removeEventListener('controllerchange', check);
          resolve();
        }
        worker.addEventListener('controllerchange', check);
        check();
      });
    }
    if (notice) notice.remove(), notice = null;
  })();
  ready.catch(() => showNotice('无法启用在线记录查看，案例和音频仍可使用。', true));

  const originalFetch = window.fetch.bind(window);
  window.fetch = async function(input, init) {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, document.baseURI);
    if (isRecord(url)) await ready;
    return originalFetch(input, init);
  };

  document.addEventListener('click', event => {
    const element = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!element || !isRecord(new URL(element.href, document.baseURI)) || controlled()) return;
    event.preventDefault();
    showNotice('正在准备记录查看，请稍后再点击。', false);
    ready.then(() => { if (notice) notice.remove(), notice = null; })
      .catch(() => showNotice('无法启用在线记录查看，案例和音频仍可使用。', true));
  }, true);
})();
