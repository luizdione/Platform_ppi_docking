/* Fila de jobs: lista que se atualiza sozinha + log ao vivo por SSE.

   A lista vem de /api/jobs (poll leve, 3 s). O log NAO e poll: abre um
   EventSource em /api/jobs/<id>/log e recebe linha a linha ate o job terminar,
   quando o servidor manda o evento `fim` e fecha. */
(function () {
  var elLista = document.getElementById('lista');
  var elRec = document.getElementById('recursos');
  var elSecaoLog = document.getElementById('secao-log');
  var elLog = document.getElementById('log');
  var elLogTitulo = document.getElementById('log-titulo');
  var btnFechar = document.getElementById('fechar-log');
  var btnCancelar = document.getElementById('cancelar-job');

  var fonte = null;      // EventSource aberto
  var jobAberto = null;  // id do job cujo log esta na tela

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function dur(s) {
    if (s == null) { return ''; }
    if (s < 60) { return s.toFixed(0) + ' s'; }
    if (s < 3600) { return (s / 60).toFixed(1) + ' min'; }
    return (s / 3600).toFixed(1) + ' h';
  }

  function pintaRecursos(r) {
    if (!r || !elRec) { return; }
    var html = '';
    var ram = r.ram || {};
    if (ram.fracao != null) {
      var usada = Math.round((1 - ram.fracao) * 100);
      var alerta = ram.fracao < 0.20 ? ' alerta' : '';
      html += '<div class="rec' + alerta + '">RAM <b>' + usada + '%</b> usada · ' +
        esc(ram.livre_gb) + ' GB livre de ' + esc(ram.total_gb) + ' GB</div>';
    }
    var gpu = r.gpu || {};
    if (gpu.sabe) {
      // A medida e a memoria de video ocupada: no Windows a lista de processos
      // inclui cliente grafico (explorer, menu iniciar) e nao diz nada sobre
      // haver trabalho CUDA em curso.
      var memGpu = gpu.memoria_mib != null
        ? ' · ' + esc(gpu.memoria_mib) + '/' + esc(gpu.total_mib) + ' MiB · ' +
          esc(gpu.uso_pct) + '% de uso'
        : '';
      html += '<div class="rec' + (gpu.ocupada ? ' alerta' : '') + '">GPU <b>' +
        (gpu.ocupada ? 'ocupada' : 'livre') + '</b>' + memGpu + '</div>';
    } else {
      html += '<div class="rec">GPU <b>?</b> · ' + esc(gpu.motivo || 'sem leitura') + '</div>';
    }
    Object.keys(r.classes || {}).forEach(function (c) {
      var o = r.classes[c];
      html += '<div class="rec">' + esc(c) + ' <b>' + o.rodando + '/' + o.limite +
        '</b>' + (o.na_fila ? ' · ' + o.na_fila + ' na fila' : '') + '</div>';
    });
    elRec.innerHTML = html;
  }

  function pintaLista(jobs) {
    if (!elLista) { return; }
    if (!jobs.length) {
      elLista.innerHTML = '<p class="mut">Nenhum job ainda. Dispare um pela pagina de um subprojeto.</p>';
      return;
    }
    elLista.innerHTML = jobs.map(function (j) {
      var espera = (j.meta && j.meta.espera) ? '<div class="mut" style="font-size:.76rem">esperando: ' + esc(j.meta.espera) + '</div>' : '';
      var erro = j.erro ? '<div class="mut" style="font-size:.76rem;color:#ff7b72">' + esc(j.erro) + '</div>' : '';
      return '<div class="job">' +
        '<span class="st st-' + esc(j.estado) + '">' + esc(j.estado) + '</span>' +
        '<div style="flex:1">' +
          '<div class="nome">' + esc(j.nome) + '</div>' +
          '<div class="mut" style="font-size:.76rem">' + esc(j.classe) +
            (j.duracao_s != null ? ' · ' + dur(j.duracao_s) : '') +
            (j.rc != null ? ' · rc=' + j.rc : '') + '</div>' +
          espera + erro +
        '</div>' +
        '<button class="acao" data-log="' + esc(j.id) + '">Log</button>' +
        '</div>';
    }).join('');
    elLista.querySelectorAll('[data-log]').forEach(function (b) {
      b.addEventListener('click', function () { abrirLog(b.getAttribute('data-log')); });
    });
  }

  function abrirLog(id) {
    fecharLog();
    jobAberto = id;
    elLog.textContent = '';
    elLogTitulo.textContent = '· ' + id;
    elSecaoLog.style.display = 'block';
    elSecaoLog.scrollIntoView({ behavior: 'smooth', block: 'start' });

    fonte = new EventSource('/api/jobs/' + encodeURIComponent(id) + '/log');
    fonte.onmessage = function (ev) {
      var linha;
      try { linha = JSON.parse(ev.data); } catch (e) { linha = ev.data; }
      var colado = (elLog.scrollTop + elLog.clientHeight) >= (elLog.scrollHeight - 30);
      elLog.textContent += linha + '\n';
      if (colado) { elLog.scrollTop = elLog.scrollHeight; }   // so segue se ja estava no fim
    };
    fonte.addEventListener('fim', function (ev) {
      var d = {};
      try { d = JSON.parse(ev.data); } catch (e) { /* ignora */ }
      elLog.textContent += '\n--- ' + (d.estado || 'fim') +
        (d.rc != null ? ' (rc=' + d.rc + ')' : '') + ' ---\n';
      elLog.scrollTop = elLog.scrollHeight;
      fecharFonte();
      atualizar();
    });
    fonte.onerror = function () { fecharFonte(); };
  }

  function fecharFonte() {
    if (fonte) { fonte.close(); fonte = null; }
  }

  function fecharLog() {
    fecharFonte();
    jobAberto = null;
    elSecaoLog.style.display = 'none';
  }

  if (btnFechar) { btnFechar.addEventListener('click', fecharLog); }
  if (btnCancelar) {
    btnCancelar.addEventListener('click', function () {
      if (!jobAberto) { return; }
      if (!window.confirm('Cancelar este job? O processo sera morto.')) { return; }
      fetch('/api/jobs/' + encodeURIComponent(jobAberto) + '/cancelar', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function () { atualizar(); })
        .catch(function () { /* a lista mostra o estado real no proximo ciclo */ });
    });
  }

  function atualizar() {
    fetch('/api/jobs')
      .then(function (r) { return r.json(); })
      .then(function (d) { pintaLista(d.jobs || []); pintaRecursos(d.recursos); })
      .catch(function () { /* servidor caiu: o proximo ciclo tenta de novo */ });
  }

  atualizar();
  setInterval(atualizar, 3000);
})();
