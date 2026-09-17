import { existsSync, realpathSync, statSync } from 'node:fs'
import { extname, isAbsolute, join, relative, resolve, sep } from 'node:path'

const PREVIEWABLE_EXTS = new Set([
  '.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi',
  '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg',
  '.jpg', '.jpeg', '.png', '.webp'
])

const MANAGED_MEDIA_ROOTS = [
  ['data', 'downloads'],
  ['data', 'capture_segments'],
  ['data', 'tts'],
  ['data', 'seedance'],
  ['exports'],
  ['previews'],
  ['.cache']
]

const MANAGED_GENERIC_FILE_ROOTS = [
  ['previews'],
  ['.cache'],
  ['data', 'seedance', 'cache']
]

function inside(directory, targetPath) {
  const relation = relative(resolve(directory), resolve(targetPath))
  return relation === '' || (
    relation !== '..'
    && !relation.startsWith(`..${sep}`)
    && !isAbsolute(relation)
  )
}

function managedRoots(root) {
  return MANAGED_MEDIA_ROOTS.map((parts) => join(root, ...parts))
}

function genericFileRoots(root) {
  return MANAGED_GENERIC_FILE_ROOTS.map((parts) => join(root, ...parts))
}

/** Viewing/revealing an imported media file is safe and intentionally supports external paths. */
export function ensureInspectableMediaPath(root, targetPath) {
  const resolved = resolve(String(targetPath || ''))
  if (managedRoots(root).some((allowed) => inside(allowed, resolved))) return resolved
  if (PREVIEWABLE_EXTS.has(extname(resolved).toLowerCase())) return resolved
  throw new Error('Only media files can be opened or revealed')
}

/** Physical deletion is limited to real files inside an app-managed media directory.
 *
 * Both the lexical and real paths are checked. The second check prevents a symlink placed under
 * exports from turning an apparently managed path into deletion of an operator-owned file.
 */
export function ensureDeletableManagedPath(root, targetPath, allowCompanion = false) {
  const resolved = resolve(String(targetPath || ''))
  const allowedRoot = managedRoots(root).find((candidate) => inside(candidate, resolved))
  if (!allowedRoot || !existsSync(resolved) || !existsSync(allowedRoot)) {
    throw new Error('Only app-managed media files can be moved to the trash')
  }
  if (!statSync(resolved).isFile()) {
    throw new Error('Only app-managed media files can be moved to the trash')
  }

  const suffix = extname(resolved).toLowerCase()
  const isKnownCompanion = suffix === '.ass' || suffix === '.json'
  const isGenericManagedFile = genericFileRoots(root).some((candidate) => inside(candidate, resolved))
  if (!PREVIEWABLE_EXTS.has(suffix) && !isGenericManagedFile && !(allowCompanion && isKnownCompanion)) {
    throw new Error('Only app-managed media files can be moved to the trash')
  }

  const canonicalRoot = realpathSync(allowedRoot)
  const canonicalTarget = realpathSync(resolved)
  if (!inside(canonicalRoot, canonicalTarget)) {
    throw new Error('Only app-managed media files can be moved to the trash')
  }
  return resolved
}
