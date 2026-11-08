(function(){
  var PAL=['#4c8bf5','#f28e2b','#59a14f','#e15759','#b07aa1','#76b7b2'];
  var IONS=['MG','ZN','CA','MN','NA','K','FE','CU','CO','NI'];
  var data=window.__PAIRS__||{pairs:[]};
  var sel=document.getElementById('pairsel');
  var kv=document.getElementById('kv');
  var vp=document.getElementById('viewport');
  var btnpng=document.getElementById('btnpng');
  var btnpdb=document.getElementById('btnpdb');
  var btncif=document.getElementById('btncif');
  var legend=document.getElementById('plddtlegend');
  var viewer=null, cur=null, mode='chain', txtCache={};
  data.pairs.forEach(function(p,i){
    var o=document.createElement('option');
    o.value=i; o.textContent=p.isca+'  x  '+p.parceiro+'   ['+p.classe+']';
    sel.appendChild(o);
  });
  function ensureViewer(){
    if(viewer) return viewer;
    if(!window.$3Dmol){ vp.innerHTML='<div style="color:#8b9bb0;padding:16px">'+
      '3Dmol.js indisponivel / unavailable.</div>'; return null; }
    viewer=$3Dmol.createViewer(vp,{backgroundColor:'#0b1220'});
    return viewer;
  }
  function fmt(x,n){ return (x==null||isNaN(x))?'-':Number(x).toFixed(n); }
  // aplica o modo de cor no modelo JA carregado (sem refetch)
  function applyStyle(v,p){
    v.setStyle({}, {cartoon:{color:'#4a5568'}});
    var chains=Object.keys(p.iface_chain_sizes||{}).sort();
    if(mode==='plddt'){
      // b-factor = pLDDT (0-100 no .cif do Boltz). roygb: min=vermelho(baixo),
      // max=azul(alto) -> convencao AlphaFold (azul = alta confianca).
      v.setStyle({}, {cartoon:{colorscheme:{prop:'b', gradient:'roygb', min:50, max:90}}});
    } else {
      chains.forEach(function(c,i){ v.setStyle({chain:c},{cartoon:{color:PAL[i%PAL.length]}}); });
    }
    if(mode==='iface'){
      var ib=p.iface_by_chain||{};
      Object.keys(ib).forEach(function(c){
        if(ib[c] && ib[c].length){ v.addStyle({chain:c,resi:ib[c]},{stick:{radius:0.24}}); }
      });
    }
    v.addStyle({hetflag:true},{stick:{radius:0.16}});
    v.addStyle({resn:IONS},{sphere:{scale:0.35}});
    legend.style.display=(mode==='plddt')?'block':'none';
  }
  function draw(p){
    var v=ensureViewer(); if(!v) return;
    v.clear(); v.addModel(txtCache[p.label],'cif');
    applyStyle(v,p); v.zoomTo(); v.render();
  }
  function render(p){
    var v=ensureViewer(); if(!v) return;
    if(txtCache[p.label]){ draw(p); return; }
    fetch(p.cif).then(function(r){ if(!r.ok) throw new Error(r.status); return r.text();})
    .then(function(txt){ txtCache[p.label]=txt; draw(p);
    }).catch(function(e){
      vp.innerHTML='<div style="color:#e0a020;padding:16px">'+
        'Falha ao carregar a estrutura / failed to load structure ('+e+').<br>'+
        'Sirva por HTTP - abrir o arquivo direto do disco pode ser bloqueado pelo navegador.</div>';
    });
  }
  // toggle de modo de cor
  document.querySelectorAll('.cmode').forEach(function(b){
    b.addEventListener('click',function(){
      mode=b.getAttribute('data-mode');
      document.querySelectorAll('.cmode').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      if(cur){ var v=ensureViewer(); if(v){ v.clear(); v.addModel(txtCache[cur.label],'cif'); applyStyle(v,cur); v.zoomTo(); v.render(); } }
    });
  });
  function badge(c){ return '<span class="badge b-'+c+'">'+c+'</span>'; }
  function show(i){
    var p=data.pairs[i]; cur=p;
    btnpdb.href=p.pdb; btnpdb.setAttribute('download', p.label+'.pdb');
    btncif.href=p.cif; btncif.setAttribute('download', p.label+'_model_0.cif');
    var chsz=p.iface_chain_sizes||{}; var chtxt=Object.keys(chsz).sort().map(function(c){
      return c+' ('+chsz[c]+')'; }).join(', ');
    kv.innerHTML =
      '<div>'+badge(p.classe)+' <b>'+p.isca+' x '+p.parceiro+'</b></div>'+
      '<div>ipTM <b>'+fmt(p.iptm,3)+'</b> &middot; iface-ipTM <b>'+fmt(p.iface_iptm,3)+
      '</b> &middot; ipSAE <b>'+fmt(p.ipsae,3)+'</b>'+
      (p.ipsae_src==='funil'?' <span style="color:#8b9bb0">(funil)</span>':'')+'</div>'+
      '<div>pTM '+fmt(p.ptm,3)+' &middot; conf. '+fmt(p.confidence_score,3)+
      ' &middot; pLDDT '+fmt(p.complex_plddt,1)+' (iface '+fmt(p.iface_plddt,1)+')</div>'+
      '<div>PAE interface '+fmt(p.pae_iface_mean,2)+' A &middot; BSA '+fmt(p.bsa,0)+
      ' A&sup2;</div>'+
      '<div>contatos/contacts <b>'+p.n_contacts+'</b> &middot; res. interface '+
      p.n_iface_res+'</div>'+
      '<div>cadeias/chains: '+chtxt+(p.iface_hetero_chains&&p.iface_hetero_chains.length?
      ' &middot; cofatores '+p.iface_hetero_chains.join(','):'')+'</div>';
    render(p);
  }
  sel.addEventListener('change',function(){ show(parseInt(sel.value,10)); });
  btnpng.addEventListener('click',function(){
    var v=ensureViewer(); if(!v||!cur) return;
    var uri=v.pngURI(); var a=document.createElement('a');
    a.href=uri; a.download=cur.label+'.png'; document.body.appendChild(a); a.click(); a.remove();
  });
  if(data.pairs.length){ sel.value='0'; show(0); }
})();
