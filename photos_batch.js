ObjC.import('Foundation');
function run(argv) {
  var out = {ok: false, operation: null, imports: [], results: []}, active = null;
  function requireOK(v, message) { if (!v) throw new Error(message); }
  function log(v) { console.log(JSON.stringify(v)); }
  function absolute(p) { return typeof p === 'string' && p[0] === '/'; }
  try {
    requireOK(argv.length === 1 && absolute(argv[0]), 'Supply one absolute manifest path');
    var fm = $.NSFileManager.defaultManager;
    var raw = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null);
    var cfg = JSON.parse(ObjC.unwrap(raw)), op = cfg.operation, rows = cfg.items;
    out.operation = op;
    requireOK(['import', 'verify', 'export', 'set_dates'].indexOf(op) >= 0, 'Unknown operation');
    requireOK(Array.isArray(rows) && rows.length > 0 && rows.length <= 100, 'Require 1..100 items');
    var names = {}, seen = {}, dirs = {}, photos = Application('Photos');
    function disk(p, kind) {
      requireOK(absolute(p) && fm.fileExistsAtPath(p), 'Missing absolute path: ' + p);
      var a = fm.attributesOfItemAtPathError(p, null);
      requireOK(ObjC.unwrap(a.objectForKey($.NSFileType)) === kind, 'Wrong file type: ' + p);
    }
    rows.forEach(function(x) {
      requireOK(x && typeof x.album === 'string' && x.album.length > 0, 'Missing QQ album name');
      x.task_album = x.album.indexOf('迁移QQ相册之“') === 0 ? x.album : '迁移QQ相册之“' + x.album + '”';
      var key = op === 'import' ? x.path : x.asset_id;
      requireOK(typeof key === 'string' && key.length > 0 && !seen[key], 'Missing/duplicate path or ID');
      seen[key] = true;
      if (op === 'import') { requireOK(!x.asset_id, 'Import items must not contain old asset IDs'); disk(x.path, 'NSFileTypeRegular'); }
      if (op === 'set_dates') {
        requireOK(typeof x.date === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.test(x.date), 'Require ISO date with explicit offset');
        requireOK(isFinite(Date.parse(x.date)), 'Invalid date: ' + x.date);
      }
      if (op === 'export') {
        x.export_dir = x.export_dir || cfg.export_dir;
        disk(x.export_dir, 'NSFileTypeDirectory');
        requireOK(!dirs[x.export_dir], 'Use a distinct export directory for each asset');
        requireOK(ObjC.unwrap(fm.contentsOfDirectoryAtPathError(x.export_dir, null)).length === 0, 'Export directory must be empty');
        dirs[x.export_dir] = true;
      }
      (names[x.task_album] || (names[x.task_album] = [])).push(x);
    });
    var albums = {}, albumInfo = {};
    function refreshAlbum(name) { var a = albums[name], ids = Object.create(null); a.mediaItems.id().forEach(function(id) { ids[id] = true; }); albumInfo[name] = {id: a.id(), members: ids}; }
    Object.keys(names).forEach(function(name) {
      var found = photos.albums.whose({name: name})();
      requireOK(found.length <= 1, 'Ambiguous duplicate album: ' + name);
      requireOK(found.length === 1 || op === 'import', 'Task album not found: ' + name);
      albums[name] = found.length ? found[0] : null;
      if (albums[name]) refreshAlbum(name);
    });
    if (op === 'import') Object.keys(names).forEach(function(name) {
      var expected = Object.create(null);
      names[name].forEach(function(x) { var f = x.path.split('/').pop(); requireOK(!expected[f], 'Import filenames must be unique per album'); expected[f] = x; });
    });
    if (op !== 'import') rows.forEach(function(x) {
      requireOK(albumInfo[x.task_album].members[x.asset_id], 'ID is not in specified task album: ' + x.asset_id);
    });
    function inspect(m, x, album) {
      var v = m.properties(), d = v.date, z = function(n) { return ('0' + n).slice(-2); };
      return {source_id: x.source_id || null, path: x.path || null, asset_id: v.id, filename: v.filename,
        epoch_seconds: d.getTime() / 1000, date_iso_utc: d.toISOString(),
        date_local: d.getFullYear() + '-' + z(d.getMonth() + 1) + '-' + z(d.getDate()) + ' ' + z(d.getHours()) + ':' + z(d.getMinutes()) + ':' + z(d.getSeconds()),
        local_utc_offset_minutes: -d.getTimezoneOffset(), width: v.width, height: v.height, size: v.size,
        album: x.album, task_album: x.task_album, album_id: albumInfo[x.task_album].id, in_album: !!albumInfo[x.task_album].members[v.id]};
    }
    if (op === 'import') Object.keys(names).forEach(function(name) {
      var group = names[name], album = albums[name], expected = Object.create(null);
      group.forEach(function(x) { expected[x.path.split('/').pop()] = x; });
      active = {task_album: name, paths: group.map(function(x) { return x.path; })}; log({event: 'import_started', group: active});
      if (!album) { album = photos.make({new: 'album', named: name}); albums[name] = album; }
      var imported = photos.import(group.map(function(x) { return Path(x.path); }), {into: album, skipCheckDuplicates: cfg.skip_check_duplicates === true});
      active.returned = imported.map(function(m) { var v = m.properties(); return {asset_id: v.id, filename: v.filename}; });
      out.imports.push(active); log({event: 'import_returned', group: active});
      requireOK(imported.length === group.length && new Set(active.returned.map(function(m) { return m.asset_id; })).size === group.length, 'Import count/unique IDs differ; reconcile before retry');
      if (cfg.defer_import_mapping !== true) active.returned.forEach(function(m) { var x = expected[m.filename]; requireOK(x && !x.asset_id, 'Unexpected/duplicate imported filename'); x.asset_id = m.asset_id; });
      refreshAlbum(name);
    });
    if (!(op === 'import' && cfg.defer_import_mapping === true)) rows.forEach(function(x, i) {
      active = {index: i, source_id: x.source_id || null, path: x.path || null, asset_id: x.asset_id || null, task_album: x.task_album};
      log({event: 'started', operation: op, item: active});
      var album = albums[x.task_album], media = photos.mediaItems.byId(x.asset_id), result;
      result = inspect(media, x, album);
      if (x.expected_filename) requireOK(result.filename === x.expected_filename, 'Filename does not match expected asset');
      if (op === 'verify' && x.expected_date) { result.date_matches = result.epoch_seconds === Date.parse(x.expected_date) / 1000; }
      if (op === 'set_dates') {
        var before = result;
        log({event: 'date_before', before: before, requested_date: x.date});
        media.date = new Date(x.date);
        result = inspect(media, x, album);
        result.before = before; result.requested_date = x.date;
        result.date_matches = result.epoch_seconds === Date.parse(x.date) / 1000;
      }
      if (op === 'export') { photos.export([media], {to: Path(x.export_dir), usingOriginals: true}); result.export_dir = x.export_dir; }
      out.results.push(result); log({event: 'completed', operation: op, result: result});
      requireOK(result.in_album && result.date_matches !== false, 'Read-back verification failed');
      active = null;
    });
    out.album_counts = Object.keys(albumInfo).map(function(name) { return {task_album: name, album_id: albumInfo[name].id, count: Object.keys(albumInfo[name].members).length}; }); out.mapping_deferred = op === 'import' && cfg.defer_import_mapping === true; out.ok = true;
  } catch (e) { out.error = String(e); out.uncertain_item = active; out.do_not_blindly_retry = out.operation === 'import' || out.operation === 'export'; }
  return JSON.stringify(out);
}
