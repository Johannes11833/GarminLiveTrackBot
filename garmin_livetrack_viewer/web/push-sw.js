self.addEventListener('install', () => {
  // Activate immediately so push subscriptions can be created without
  // waiting for a reload (an older worker may still control the tab).
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = { body: event.data ? event.data.text() : '' };
  }
  const title = payload.title || 'Garmin LiveTrack';
  const options = {
    body: payload.body || '',
    icon: 'icons/Icon-192.png',
    badge: 'icons/Icon-192.png',
    tag: payload.tag || 'livetrack',
    data: {
      sessionId: payload.sessionId || '',
      sessionToken: payload.sessionToken || '',
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const sessionId = data.sessionId || '';
  const sessionToken = data.sessionToken || '';
  let search = '';
  if (sessionId) {
    search = '?id=' + encodeURIComponent(sessionId);
    if (sessionToken) search += '&sessionToken=' + encodeURIComponent(sessionToken);
  }
  const target = new URL(search || '.', self.registration.scope);
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        const url = new URL(client.url);
        if (url.pathname === target.pathname) {
          url.search = target.search;
          return client.navigate(url.href).then(() => client.focus());
        }
      }
      return clients.openWindow(target.href);
    }),
  );
});
