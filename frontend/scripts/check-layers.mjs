/**
 * The dependency rule, made executable — the frontend counterpart of
 * `backend/tests/test_domain_isolation.py`.
 *
 * Layering that is only written down decays; layering a command can fail does
 * not. Every import inside `src/<layer>/` is checked against what that layer is
 * allowed to reach, and a violation exits non-zero.
 *
 *   npm run check:layers
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

/**
 * `layers` lists the directories a layer may import from — always itself and
 * whatever lies inward. `packages` lists the bare specifiers it may import.
 *
 * `domain` may reach nothing at all: no framework, no sibling layer. `application`
 * gets React because a use case in a React application is a hook, and that is
 * the whole of the concession.
 */
const RULES = {
  domain: { layers: ['domain'], packages: [] },
  application: { layers: ['domain', 'application'], packages: ['react'] },
  infrastructure: { layers: ['domain', 'infrastructure'], packages: [] },
  presentation: {
    layers: ['domain', 'application', 'presentation'],
    packages: ['react'],
  },
}

/** `from '…'` and bare side-effect `import '…'`. */
const IMPORT_PATTERN = /\bfrom\s*['"]([^'"]+)['"]|\bimport\s*['"]([^'"]+)['"]/g

const violations = []

for (const [layer, rule] of Object.entries(RULES)) {
  for (const file of sourceFiles(join(SRC, layer))) {
    const source = readFileSync(file, 'utf8')

    for (const match of source.matchAll(IMPORT_PATTERN)) {
      const specifier = match[1] ?? match[2]
      const problem = check(specifier, file, rule)
      if (problem !== null) {
        violations.push(`${relative(SRC, file).replaceAll(sep, '/')}: ${problem}`)
      }
    }
  }
}

if (violations.length > 0) {
  console.error(`Dependency rule violated in ${String(violations.length)} place(s):\n`)
  for (const violation of violations) console.error(`  ${violation}`)
  console.error('\nSee scripts/check-layers.mjs for what each layer may reach.')
  process.exit(1)
}

console.log(`Dependency rule holds across ${Object.keys(RULES).join(', ')}.`)

/** @returns a description of the problem, or null if the import is allowed. */
function check(specifier, file, rule) {
  // A stylesheet is a presentation concern and belongs to the component that
  // imports it; the layers it can reach are not in question.
  if (specifier.endsWith('.css')) {
    return rule.layers.includes('presentation')
      ? null
      : `imports the stylesheet '${specifier}', which only presentation may do`
  }

  if (!specifier.startsWith('.')) {
    const root = specifier.startsWith('@')
      ? specifier.split('/').slice(0, 2).join('/')
      : (specifier.split('/')[0] ?? specifier)
    return rule.packages.includes(root)
      ? null
      : `imports the package '${specifier}', which this layer may not use`
  }

  const target = relative(SRC, resolve(dirname(file), specifier))
  if (target.startsWith('..')) {
    return `imports '${specifier}', which leaves src/`
  }

  const targetLayer = target.split(sep)[0]
  return rule.layers.includes(targetLayer)
    ? null
    : `imports '${specifier}' from the ${targetLayer} layer`
}

function sourceFiles(directory) {
  const found = []
  for (const name of readdirSync(directory)) {
    const path = join(directory, name)
    if (statSync(path).isDirectory()) {
      found.push(...sourceFiles(path))
    } else if (/\.tsx?$/.test(name)) {
      found.push(path)
    }
  }
  return found
}
