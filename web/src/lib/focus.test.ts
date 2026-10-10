/**
 * Unit tests for src/lib/focus.ts, the focus helpers of in-page navigation (WCAG 2.2 SC 2.4.3).
 *
 * - `idFromHash` strips the `#`, decodes percent-encoding, keeps a malformed fragment as written
 *   and reads an empty fragment as no id.
 * - `focusElement` focuses an element that is not focusable by itself through a transient
 *   `tabindex="-1"`, removed on blur or at once when focus is refused, and focuses a natively
 *   focusable element, or one with a `tabindex` of its own, without touching its attributes.
 *
 * The elements are built with the DOM API, in jsdom, and removed after each test.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { focusElement, idFromHash } from './focus';

afterEach(() => {
  document.body.replaceChildren();
});

/**
 * Appends a new element to `<body>`.
 *
 * @param tag - the element's tag name
 * @param attributes - attributes to set on it
 * @returns the element
 */
function append(tag: string, attributes: Record<string, string> = {}): HTMLElement {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    element.setAttribute(name, value);
  }
  document.body.append(element);
  return element;
}

describe('idFromHash', () => {
  it('reads the id a fragment names, decoded', () => {
    expect(idFromHash('#section-keywords')).toBe('section-keywords');
    expect(idFromHash('section-keywords')).toBe('section-keywords');
    expect(idFromHash('#item-%37%35')).toBe('item-75');
    expect(idFromHash('##item-75')).toBe('#item-75');
  });

  it('keeps a malformed fragment as written', () => {
    expect(idFromHash('#item-%E0%A4%A')).toBe('item-%E0%A4%A');
  });

  it('reads an empty fragment as no id', () => {
    expect(idFromHash('')).toBeNull();
    expect(idFromHash('#')).toBeNull();
  });
});

describe('focusElement', () => {
  it('focuses a region through a tabindex it removes again on blur', () => {
    const section = append('section', { id: 'section-keywords' });
    const other = append('button');

    expect(focusElement(section, { preventScroll: true })).toBe(true);
    expect(document.activeElement).toBe(section);
    expect(section.getAttribute('tabindex')).toBe('-1');

    // Focusing it again while it holds focus keeps the one transient attribute.
    expect(focusElement(section)).toBe(true);
    expect(section.getAttribute('tabindex')).toBe('-1');

    other.focus();
    expect(section.hasAttribute('tabindex')).toBe(false);
  });

  it('removes the tabindex at once when the element cannot take focus', () => {
    const detached = document.createElement('section');

    expect(focusElement(detached)).toBe(false);
    expect(detached.hasAttribute('tabindex')).toBe(false);
    expect(document.activeElement).toBe(document.body);
  });

  it('focuses a natively focusable element, or one with its own tabindex, as it is', () => {
    const button = append('button');
    const main = append('main', { tabindex: '-1' });

    expect(focusElement(button)).toBe(true);
    expect(document.activeElement).toBe(button);
    expect(button.hasAttribute('tabindex')).toBe(false);

    expect(focusElement(main)).toBe(true);
    expect(document.activeElement).toBe(main);
    button.focus();
    expect(main.getAttribute('tabindex')).toBe('-1');
  });

  it('reports a focusable element that refuses focus', () => {
    const disabled = append('button', { disabled: '' });

    expect(focusElement(disabled)).toBe(false);
    expect(document.activeElement).toBe(document.body);
  });
});
