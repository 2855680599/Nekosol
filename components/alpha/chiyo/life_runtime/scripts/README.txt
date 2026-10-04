Placeholder directory.

The real canonical owners (LR/AR/AG modules) were copied flat into
../  (chiyo/life_runtime/) so that every module only imports stdlib plus its
local siblings.  The canonical AG-0 / AG-1 / AR-0 / AR-1 loaders resolve their
runtime root and then require <runtime_root>/scripts to be a directory before
putting it on sys.path, so this directory exists to satisfy that contract and
is intentionally empty.

CHIYO_CANONICAL_RUNTIME_ROOT / CT0_10_RUNTIME / CT0_10_RUNTIME_ROOT all point
at ../  (chiyo/life_runtime), so the flat modules are found on sys.path.
