import hashlib
import os
import re
NATIVE_PAIR = re.compile('[0-9A-Fa-f]{8}_[0-9A-Fa-f]{8}\\.(so|dylib|dll|ranges)$')
CACHE = 'cache'
SIZE_ONLY = ('inputs',)

def _mark(path, relative):
    if relative.split('/', 1)[0] in SIZE_ONLY:
        return 'size:%d' % os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()

def _walk(product):
    files, folders = ([], [])
    for current, names, leaves in os.walk(product):
        base = os.path.relpath(current, product)
        prefix = '' if base == '.' else base.replace(os.sep, '/') + '/'
        for name in list(names):
            if os.path.islink(os.path.join(current, name)):
                names.remove(name)
                files.append(prefix + name)
            else:
                folders.append(prefix + name)
        files.extend((prefix + name for name in leaves))
    return (sorted(files), sorted(folders))

def snapshot(product):
    product = str(product)
    files, folders = _walk(product)
    marks = {}
    for relative in files:
        path = os.path.join(product, *relative.split('/'))
        marks[relative] = 'link' if os.path.islink(path) else _mark(path, relative)
    return {'files': marks, 'folders': folders}

def drop_bytecode(product):
    removed = 0
    for current, names, _leaves in os.walk(str(product)):
        for name in list(names):
            if name == '__pycache__' and (not os.path.islink(os.path.join(current, name))):
                folder = os.path.join(current, name)
                for inner, _dirs, files in os.walk(folder, topdown=False):
                    for leaf in files:
                        os.remove(os.path.join(inner, leaf))
                    os.rmdir(inner)
                names.remove(name)
                removed += 1
    return removed

def is_native_pair(relative):
    parts = relative.split('/')
    return parts[0] == CACHE and len(parts) > 1 and bool(NATIVE_PAIR.match(parts[-1]))

def changed(product, before):
    product = str(product)
    present = set(_walk(product)[0])
    found = []
    for relative, mark in sorted(before['files'].items()):
        path = os.path.join(product, *relative.split('/'))
        if relative not in present:
            found.append(relative + ' (gone)')
        elif mark != ('link' if os.path.islink(path) else _mark(path, relative)):
            found.append(relative + ' (changed)')
    return found

def clean(product, before):
    product = str(product)
    files, _folders = _walk(product)
    removed, kept = ([], [])
    for relative in files:
        if relative in before['files']:
            continue
        if is_native_pair(relative):
            kept.append(relative)
            continue
        os.remove(os.path.join(product, *relative.split('/')))
        removed.append(relative)
    different = changed(product, before)
    known = set(before['folders'])
    for relative in sorted(_walk(product)[1], key=lambda name: name.count('/'), reverse=True):
        path = os.path.join(product, *relative.split('/'))
        if relative not in known and (not os.listdir(path)):
            os.rmdir(path)
    return {'removed': removed, 'kept_cache': kept, 'changed': different}

def unexpected(product, before):
    files, _folders = _walk(str(product))
    return [relative for relative in files if relative not in before['files'] and (not is_native_pair(relative))]
