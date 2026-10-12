/* Display preferences only. Never changes channel registration or automation. */
(function (root) {
  'use strict';
  const key = 'automationStudioHiddenChannels.v1';
  const valid = id => typeof id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(id) && id !== 'all';
  function read() {
    try {
      const value = JSON.parse(root.localStorage.getItem(key) || '[]');
      return new Set(Array.isArray(value) ? value.filter(valid) : []);
    } catch (_) { return new Set(); }
  }
  const idOf = channel => channel.id || channel.channel_id || '';
  root.ChannelVisibility = Object.freeze({
    isHidden(id) { return read().has(id); },
    filter(channels, protectedIds = []) {
      const hidden = read();
      return channels.filter(c => protectedIds.includes(idOf(c)) || !hidden.has(idOf(c)));
    },
    setHidden(id, hidden) {
      if (!valid(id)) throw new Error('チャンネルIDが不正です');
      const ids = read();
      if (hidden) ids.add(id); else ids.delete(id);
      // Let storage failures reach the UI; never report a save that failed.
      root.localStorage.setItem(key, JSON.stringify([...ids].sort()));
    }
  });
  root.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      root.dispatchEvent(new root.Event('channel-visibility-change'));
    }
  });
})(window);
