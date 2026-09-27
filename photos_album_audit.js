// Read-only exact album identity/member audit; never imports or edits Photos.
ObjC.import('Foundation');
function run(argv) {
  var out = {ok: false, checked_at_utc: new Date().toISOString(), albums: []};
  try {
    if (argv.length !== 1 || argv[0][0] !== '/') throw Error('Supply one absolute request path');
    var raw = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null);
    var cfg = JSON.parse(ObjC.unwrap(raw)), photos = Application('Photos'), seen = {};
    if (!Array.isArray(cfg.albums)) throw Error('Missing album request');
    cfg.albums.forEach(function(group) {
      if (!group.task_album || !group.album_id || seen[group.task_album] || !Array.isArray(group.expected_ids))
        throw Error('Invalid or duplicate album request');
      seen[group.task_album] = true;
      var found = photos.albums.whose({name: group.task_album})();
      if (found.length !== 1 || found[0].id() !== group.album_id) throw Error('Album identity changed');
      var ids = found[0].mediaItems.id(), actual = new Set(ids), expected = new Set(group.expected_ids);
      if (expected.size !== group.expected_ids.length) throw Error('Duplicate expected member');
      out.albums.push({task_album: group.task_album, album_id: group.album_id, member_ids: ids,
        count: ids.length, expected_count: group.expected_ids.length,
        missing: group.expected_ids.filter(function(id) { return !actual.has(id); }),
        extra: ids.filter(function(id) { return !expected.has(id); })});
    });
    out.ok = true;
  } catch (error) { out.error = String(error); }
  return JSON.stringify(out);
}
