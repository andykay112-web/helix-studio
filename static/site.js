const menu=document.querySelector('.menu-button');
const nav=document.querySelector('.nav');
if(menu&&nav){menu.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')==='true';menu.setAttribute('aria-expanded',String(!open));nav.classList.toggle('open',!open)});nav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{nav.classList.remove('open');menu.setAttribute('aria-expanded','false')}));}
const methods=document.getElementById('payment-method');
if(methods){const update=()=>{document.querySelectorAll('[data-method]').forEach(el=>el.hidden=el.getAttribute('data-method')!==methods.value);const issuer=document.querySelector('[name="gift_brand"]');if(issuer)issuer.required=methods.value==='gift_card';};methods.addEventListener('change',update);update();}
