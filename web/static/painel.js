/* Desenha os graficos de confianca do subprojeto.

   O servidor manda as figuras ja prontas em `window.__CHARTS__` (JSON do
   Plotly, uma entrada por grafico) e aqui so plotamos, com o tema escuro do
   painel. Sem rede: o plotly.min.js e servido localmente. */
(function () {
  var dados = window.__CHARTS__ || {};
  var alvo = document.getElementById('charts');
  var vazio = document.getElementById('charts-vazio');
  if (!alvo) { return; }

  var nomes = Object.keys(dados);
  if (!nomes.length) {
    if (vazio) { vazio.style.display = 'block'; }
    return;
  }
  if (typeof Plotly === 'undefined') {
    alvo.innerHTML = '<p class="mut">plotly.js indisponivel - grafico omitido ' +
      '(a tabela acima continua valendo).</p>';
    return;
  }

  var LAYOUT = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: '#e8f4fa', family: "'Segoe UI',Inter,system-ui,Arial,sans-serif", size: 11 },
    margin: { l: 60, r: 20, t: 40, b: 90 },
    xaxis: { gridcolor: '#1e4080', zerolinecolor: '#1e4080' },
    yaxis: { gridcolor: '#1e4080', zerolinecolor: '#1e4080' },
    legend: { bgcolor: 'rgba(0,0,0,0)' }
  };

  nomes.forEach(function (nome) {
    var fig;
    try {
      fig = (typeof dados[nome] === 'string') ? JSON.parse(dados[nome]) : dados[nome];
    } catch (e) {
      return;
    }
    var caixa = document.createElement('figure');
    caixa.className = 'chart';
    var div = document.createElement('div');
    div.id = 'chart_' + nome;
    caixa.appendChild(div);
    alvo.appendChild(caixa);

    var layout = Object.assign({}, fig.layout || {}, LAYOUT);
    // preserva titulo/eixos que a figura ja trazia
    if (fig.layout) {
      if (fig.layout.title) { layout.title = fig.layout.title; }
      if (fig.layout.xaxis) { layout.xaxis = Object.assign({}, fig.layout.xaxis, LAYOUT.xaxis); }
      if (fig.layout.yaxis) { layout.yaxis = Object.assign({}, fig.layout.yaxis, LAYOUT.yaxis); }
    }
    Plotly.newPlot(div, fig.data || [], layout, { displaylogo: false, responsive: true });
  });
})();

/* Botoes de acao da pagina do subprojeto: enfileiram um job e apontam para /jobs.
   A acao NAO roda aqui: vai para a fila do motor, que respeita as travas de
   recurso (GPU unica, RAM >=20% livre) e o log ao vivo fica na pagina de jobs. */
(function () {
  var aviso = document.getElementById('aviso-acao');

  function dispara(url, corpo, rotulo) {
    if (aviso) { aviso.style.display = 'block'; aviso.textContent = 'enfileirando ' + rotulo + '...'; }
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: corpo ? JSON.stringify(corpo) : null
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { throw new Error(res.d.error || res.d.detail || 'falhou'); }
        aviso.innerHTML = rotulo + ' na fila (job <code>' + res.d.job_id +
          '</code>) &middot; <a href="/jobs">acompanhar o log</a>';
      })
      .catch(function (e) {
        if (aviso) { aviso.style.color = 'var(--am)'; aviso.textContent = 'falhou: ' + e.message; }
      });
  }

  var bFunil = document.getElementById('btn-funil');
  if (bFunil) {
    bFunil.addEventListener('click', function () {
      dispara('/api/acoes/funil', { subprojeto: bFunil.getAttribute('data-key'), ate: 'prepare' }, 'funil');
    });
  }
  var bBuild = document.getElementById('btn-build');
  if (bBuild) {
    bBuild.addEventListener('click', function () {
      dispara('/api/acoes/build-viewer', null, 'build do viewer');
    });
  }
})();
