const CACHE='ge-fleet-v1.0.0';
const SHELL=['/','/static/index.html','/static/app.css','/static/app.js','/manifest.webmanifest','/static/icons/icon-192.png','/static/icons/icon-512.png'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',event=>{
 if(event.request.method!=='GET'||new URL(event.request.url).origin!==self.location.origin)return;
 const u=new URL(event.request.url);
 if(u.pathname.startsWith('/api/'))return; // Never cache business data silently.
 event.respondWith(fetch(event.request).then(response=>{
  if(response.ok&&(u.pathname==='/'||u.pathname.startsWith('/static/')||u.pathname==='/manifest.webmanifest')){
   const clone=response.clone();caches.open(CACHE).then(c=>c.put(event.request,clone));
  }return response;
 }).catch(()=>caches.match(event.request).then(r=>r||caches.match('/'))));
});
