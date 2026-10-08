/**
 * Accessibility helpers shared by the design-system wrappers `TextField`, `Select` and
 * `Tooltip`, so the content rule and the `aria-describedby` assembly they all depend on are
 * defined once and cannot drift apart.
 *
 * This module is internal to `src/design-system/components`: it is not re-exported from the
 * design-system index, and feature code never imports it (AAP 0.4.2).
 *
 * @packageDocumentation
 */
import type { ReactNode } from 'react';

/**
 * Whether a node renders visible content. React renders `undefined`, `null` and booleans as
 * nothing, and the empty string has no text, so none of them produces a hint or error
 * paragraph, marks a field invalid or opens a tooltip; `0` and every other node count as
 * content.
 *
 * @param node - an optional slot such as a field's `hint` or `error`, or a tooltip's `content`
 * @returns `false` for a node that would render nothing
 */
export function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * Merges id reference lists, such as the values of `aria-describedby`, into one list.
 *
 * Each list is split on whitespace and empty entries are dropped. Every id is kept once, at its
 * first occurrence, so the lists keep their order: pass them in the order assistive technology
 * should read the descriptions (for example a field's hint, then its error, then a tooltip's
 * id).
 *
 * @param lists - space-separated id lists; `undefined` entries are skipped
 * @returns the merged space-separated list, or `undefined` when it holds no id, so an empty
 *   attribute is never rendered
 */
export function mergeIdRefs(...lists: readonly (string | undefined)[]): string | undefined {
  const ids: string[] = [];
  for (const list of lists) {
    if (list === undefined) {
      continue;
    }
    for (const id of list.split(/\s+/)) {
      if (id !== '' && !ids.includes(id)) {
        ids.push(id);
      }
    }
  }
  return ids.length > 0 ? ids.join(' ') : undefined;
}
