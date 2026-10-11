/**
 * Component tests of `DecisionForm` (AAP 0.5.2 decision shapes and patch model, 0.5.3
 * `POST /decisions` and `POST /discrepancies/{id}/resolve`, 0.5.5 "Record decision" and "Record
 * intended state").
 *
 * They render the real form inside the app's providers, with only the network stubbed by
 * `../../test/mockApi`, so every assertion covers the real TanStack Query hooks and API client:
 *
 * - **Shapes.** An `own_budget` campaign offers status plus exactly the budget fields it has (a
 *   lifetime campaign no daily budget), a `shared_member` campaign status only, and a shared
 *   budget its one amount and no status. A prefill field outside the shape is never posted.
 * - **Prefill.** The form starts from the latest decision, overlaid with the recommendation's
 *   proposed change; a subject with no decision yet starts from its discrepancy.
 * - **Validation.** Amounts stop at the currency's minor unit, or at whole micros on Google,
 *   and an empty or no-change patch is refused before any request. Every current error is
 *   summarised in the form's one polite status region, which describes the disabled submit.
 * - **Requests.** The exact `POST /decisions` and `record_intended` bodies, the
 *   `X-Requested-With: ads-engine` header, `onDone` and the query invalidation after success.
 *   Total budgets are posted for a `CUSTOM_PERIOD` shared budget with no status or daily
 *   budget, and for lifetime, `CUSTOM_PERIOD` and dual-budget campaigns with only the changed
 *   fields in a patch and the complete state in an intended state.
 * - **Errors.** A 422 shows the API's message and takes focus; a 409 `subject_retired` locks
 *   the form as "Computed from replaced data"; a 409 `discrepancy_resolved` reads "Already
 *   resolved"; a failure that is not an `ApiError` reads "The save failed.", never its own
 *   message. No error takes `role="alert"`.
 * - **Outcomes.** `onPendingChange` reports `true` when a save is sent and `false` when it
 *   settles, before `onDone`, `onRetired` or `onFailed`; `onFailed` receives every failure but
 *   `subject_retired`, with the form's message; each reaches the consumer after an unmount too.
 */
// mockApi registers its afterEach check first, so it runs after Testing Library's cleanup.
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockResponse, RecordedCall } from '../../test/mockApi';
import { createTestQueryClient, renderWithProviders } from '../../test/render';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';

import { CURRENT_STATE_POLL_MS, queryKeys } from '../../api/queries';
import type {
  Decision,
  Discrepancy,
  DiscrepancyValue,
  SubjectIn,
  SubjectView,
} from '../../api/types';
import { Button } from '../../design-system';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  makeDecision,
  makeDiscrepancy,
} from '../../test/fixtures';
import { DecisionForm } from './DecisionForm';
import type { DecisionFormProps } from './DecisionForm';

// ---------------------------------------------------------------------------------------------
// Subjects
// ---------------------------------------------------------------------------------------------

/** The Google own-budget campaign of `makeDecision('own_budget')`: "Brand Search — US", USD. */
const OWN_BUDGET: SubjectIn = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch };

/** The Google shared-member campaign of `makeDecision('shared_member')`. */
const SHARED_MEMBER: SubjectIn = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.searchPartners };

/** The Google explicitly shared budget of `makeDecision('budget')`. */
const SHARED_BUDGET: SubjectIn = { type: 'budget', id: FIXTURE_BUDGET_IDS.search };

/** A LinkedIn campaign: its amounts stop at the currency's minor unit, not at micros. */
const LINKEDIN_CAMPAIGN: SubjectIn = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea };

/** The path a `record_intended` resolution of the fixture discrepancy is posted to. */
const RESOLVE_PATH = `/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`;

/**
 * The subject view of a LinkedIn campaign in `currency`.
 *
 * @param currency - The account currency.
 * @returns The subject as the decision log shows it.
 */
function linkedinSubject(currency: string): SubjectView {
  return {
    type: 'campaign',
    id: LINKEDIN_CAMPAIGN.id,
    name: 'Lead Gen — Pipeline Report EMEA',
    source: 'linkedin',
    currency,
  };
}

/**
 * The latest decision of the LinkedIn campaign, an `own_budget` row.
 *
 * @param overrides - Fields to replace.
 * @returns The decision.
 */
function linkedinDecision(overrides: Partial<Decision> = {}): Decision {
  return makeDecision('own_budget', {
    id: 811,
    subject: linkedinSubject('EUR'),
    daily_budget: '100.00',
    total_budget: '3000.00',
    ...overrides,
  });
}

/**
 * An `untracked` discrepancy on the LinkedIn campaign, an `own_budget` subject in EUR: its
 * platform side is the campaign's complete current state, `active` with these budgets, and
 * carries no micros, which only Google reports.
 *
 * @param budgets - The campaign's daily and total budgets on the platform; `null` where it has
 *   none.
 * @param overrides - Discrepancy fields to replace.
 * @returns The discrepancy.
 */
function linkedinUntracked(
  budgets: Required<Pick<DiscrepancyValue, 'daily_budget' | 'total_budget'>>,
  overrides: Partial<Discrepancy> = {},
): Discrepancy {
  return makeDiscrepancy('untracked', {
    subject: linkedinSubject('EUR'),
    platform_value: { status: 'active', ...budgets, currency: 'EUR', amount_micros: null },
    ...overrides,
  });
}

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * The handler key of one subject's decision log, with the query the form sends.
 *
 * @param subject - The decision subject.
 * @returns The `mockApi` key.
 */
function logKey(subject: SubjectIn): string {
  return `GET /decisions?subject_type=${subject.type}&subject_id=${String(subject.id)}`;
}

/**
 * The requests recorded for one method and path.
 *
 * @param api - The mock.
 * @param method - The upper-case method.
 * @param path - The path relative to `/api/v1`.
 * @returns The matching calls, in request order.
 */
function callsTo(api: ApiMock, method: string, path: string): RecordedCall[] {
  return api.calls.filter((call) => call.method === method && call.path === path);
}

/**
 * The only request recorded for one method and path.
 *
 * @throws Error when there is not exactly one.
 */
function onlyCall(api: ApiMock, method: string, path: string): RecordedCall {
  const calls = callsTo(api, method, path);
  const [call] = calls;
  if (calls.length !== 1 || call === undefined) {
    throw new Error(`expected one ${method} ${path}, got ${String(calls.length)}`);
  }
  return call;
}

/**
 * Every write recorded, whatever its path.
 *
 * @param api - The mock.
 * @returns The `POST` calls, in request order.
 */
function postsOf(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'POST');
}

/**
 * A response the test settles later, to observe the form while a request is in flight.
 *
 * @returns The pending response and the function that settles it.
 */
function deferredResponse(): {
  promise: Promise<MockResponse>;
  resolve: (response: MockResponse) => void;
} {
  let resolve: (response: MockResponse) => void = () => undefined;
  const promise = new Promise<MockResponse>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

/**
 * Renders the form at `/decisions` and waits for the initial decision-log read to settle, that
 * is, until the "Loading decision" spinner is gone. The read need not succeed: a failed read,
 * which shows the critical banner in the spinner's place, ends the wait too.
 *
 * @param props - The form's props.
 * @param options - The query client to render with; a fresh one when omitted.
 * @returns The render result, with its query client.
 */
async function renderForm(
  props: DecisionFormProps,
  options: { queryClient?: ReturnType<typeof createTestQueryClient> } = {},
) {
  const result = await renderWithProviders(<DecisionForm {...props} />, {
    path: '/decisions',
    queryClient: options.queryClient ?? createTestQueryClient(),
  });
  await waitFor(() => {
    expect(screen.queryByText('Loading decision')).toBeNull();
  });
  return result;
}

/** The status select. */
function statusSelect(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Status' });
}

/** The amount input with this label. */
function amountInput(label: 'Daily budget' | 'Total budget'): HTMLElement {
  return screen.getByRole('spinbutton', { name: label });
}

/** The note field. */
function noteField(): HTMLElement {
  return screen.getByRole('textbox', { name: 'Note' });
}

/** The submit button of the given mode. */
function saveButton(label: 'Save decision' | 'Save intended state' = 'Save decision'): HTMLElement {
  return screen.getByRole('button', { name: label });
}

/**
 * The form's one polite status region. `hidden: true` also counts status elements excluded
 * from the accessibility tree, so the check fails when the form renders a second one anywhere.
 *
 * @throws Error when there is not exactly one.
 */
function formStatus(): HTMLElement {
  const regions = screen.getAllByRole('status', { hidden: true });
  const [region] = regions;
  if (region === undefined || regions.length !== 1) {
    throw new Error(`expected one status region, found ${String(regions.length)}`);
  }
  return region;
}

/** Types `value` into a field. */
function typeInto(field: HTMLElement, value: string): void {
  fireEvent.change(field, { target: { value } });
}

/**
 * The form an element sits in.
 *
 * @throws Error when it sits in none.
 */
function formOf(element: HTMLElement): HTMLFormElement {
  const form = element.closest('form');
  if (form === null) {
    throw new Error('the element is not inside a form');
  }
  return form;
}

/** The labels of the status select's options, in order. */
function statusOptions(): (string | null)[] {
  return within(statusSelect())
    .getAllByRole('option', { hidden: true })
    .map((option) => option.textContent);
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('DecisionForm', () => {
  describe('loading and states without a form', () => {
    it("shows a spinner while the subject's decision log loads, then the form", async () => {
      const log = deferredResponse();
      const api = mockApi({ [logKey(OWN_BUDGET)]: () => log.promise });
      await renderWithProviders(<DecisionForm subject={OWN_BUDGET} />, { path: '/decisions' });

      expect(screen.getByRole('status').textContent).toBe('Loading decision');
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();

      log.resolve(json(200, [makeDecision('own_budget')]));
      expect(await screen.findByRole('button', { name: 'Save decision' })).toBeDefined();
      const read = onlyCall(api, 'GET', '/decisions');
      expect(read.search.get('subject_type')).toBe('campaign');
      expect(read.search.get('subject_id')).toBe(String(FIXTURE_CAMPAIGN_IDS.brandSearch));
    });

    it("shows the API's message when the decision log cannot be read", async () => {
      mockApi({
        [logKey(OWN_BUDGET)]: apiError(503, 'unavailable', 'The database is unavailable.'),
      });
      await renderWithProviders(<DecisionForm subject={OWN_BUDGET} />, { path: '/decisions' });

      expect(await screen.findByText('The database is unavailable.')).toBeDefined();
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('keeps a failed read of the log on screen while a poll reads it again', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const poll = deferredResponse();
        let reads = 0;
        mockApi({
          [logKey(OWN_BUDGET)]: () => {
            reads += 1;
            return reads === 1
              ? apiError(503, 'unavailable', 'The database is unavailable.')
              : poll.promise;
          },
        });
        await renderWithProviders(<DecisionForm subject={OWN_BUDGET} />, { path: '/decisions' });
        expect(await screen.findByText('The database is unavailable.')).toBeDefined();

        await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
        expect(reads).toBe(2);
        expect(screen.queryByText('Loading decision')).toBeNull();
        expect(screen.getByText('The database is unavailable.')).toBeDefined();

        poll.resolve(json(200, [makeDecision('own_budget')]));
        expect(await screen.findByRole('button', { name: 'Save decision' })).toBeDefined();
        expect(screen.queryByText('The database is unavailable.')).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('reads a 409 subject_retired on the log as "Computed from replaced data"', async () => {
      mockApi({
        [logKey(OWN_BUDGET)]: apiError(409, 'subject_retired', 'The dataset was replaced.'),
      });
      await renderWithProviders(<DecisionForm subject={OWN_BUDGET} />, { path: '/decisions' });

      expect(await screen.findByText('Computed from replaced data')).toBeDefined();
      expect(screen.queryByText('The dataset was replaced.')).toBeNull();
    });

    it('offers no form when the subject has neither a decision nor a discrepancy', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, []) });
      await renderForm({ subject: OWN_BUDGET });

      expect(screen.getByText('No decided state is recorded for this subject yet.')).toBeDefined();
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
    });

    it('takes no decision on a subject whose removal was acknowledged', async () => {
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [
          makeDecision('own_budget', { tombstone: true, status: 'ended', origin: 'resolution' }),
        ]),
      });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' } });

      expect(screen.getByText('Brand Search — US was acknowledged as removed')).toBeDefined();
      expect(screen.getByText('It takes no further decisions.')).toBeDefined();
      expect(screen.queryByRole('combobox', { name: 'Status' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
    });

    it('names an unnamed removed subject generically', async () => {
      const removed = makeDecision('own_budget', { tombstone: true, status: 'ended' });
      removed.subject.name = '  ';
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [removed]) });
      await renderForm({ subject: OWN_BUDGET });

      expect(screen.getByText('This subject was acknowledged as removed')).toBeDefined();
    });

    it('offers no form for a shared budget whose amount is unknown', async () => {
      mockApi({
        [logKey(SHARED_BUDGET)]: json(200, [makeDecision('budget', { daily_budget: null })]),
      });
      await renderForm({ subject: SHARED_BUDGET });

      expect(
        screen.getByText('No budget amount is known for this shared budget yet.'),
      ).toBeDefined();
      expect(screen.queryByRole('spinbutton')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
    });
  });

  describe('fields per decision shape (AAP 0.5.2)', () => {
    it('offers an own-budget Google campaign its status and daily budget only', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });

      expect(screen.getByText('Last decided: Active · daily budget USD 200.00')).toBeDefined();
      expect(statusSelect()).toHaveProperty('value', 'active');
      expect(statusOptions()).toEqual(['Active', 'Paused', 'Ended']);

      const daily = amountInput('Daily budget');
      expect(daily).toHaveProperty('value', '200.00');
      // Google amounts may carry whole micros, whatever the currency.
      expect(daily.getAttribute('step')).toBe('0.000001');
      expect(daily.getAttribute('min')).toBe('0');
      expect(screen.getByText('In USD')).toBeDefined();
      expect(screen.queryByRole('spinbutton', { name: 'Total budget' })).toBeNull();

      // Nothing changed yet, so nothing can be submitted, and the button says why.
      const save = saveButton();
      expect(save).toHaveProperty('disabled', true);
      const hint = screen.getByText('No change from the last decided state');
      expect(save.getAttribute('aria-describedby')).toBe(hint.id);
      expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
    });

    it('offers a LinkedIn campaign both its budgets, stepped by the minor unit', async () => {
      mockApi({ [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]) });
      await renderForm({ subject: LINKEDIN_CAMPAIGN });

      expect(
        screen.getByText('Last decided: Active · daily budget EUR 100.00 · total budget EUR 3,000.00'),
      ).toBeDefined();
      expect(amountInput('Daily budget')).toHaveProperty('value', '100.00');
      expect(amountInput('Total budget')).toHaveProperty('value', '3000.00');
      expect(amountInput('Daily budget').getAttribute('step')).toBe('0.01');
      expect(screen.getAllByText('In EUR')).toHaveLength(2);
    });

    it('offers a lifetime campaign no daily budget and drops a proposed one', async () => {
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision({ daily_budget: null })]),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN, prefill: { daily_budget: '240.00' } });

      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      expect(amountInput('Total budget')).toHaveProperty('value', '3000.00');
      // The dropped prefill is no change, so nothing can be posted.
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.getByText('No change from the last decided state')).toBeDefined();
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });

    it('offers a shared-member campaign its status only and posts no amount', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(SHARED_MEMBER)]: json(200, [makeDecision('shared_member')]),
        'POST /decisions': json(
          201,
          makeDecision('shared_member', { id: 902, status: 'paused', origin: 'recommendation' }),
        ),
      });
      await renderForm({
        subject: SHARED_MEMBER,
        prefill: { status: 'paused', daily_budget: '999.00', total_budget: '9999.00' },
        onDone,
      });

      // The budget is named by its Google id, as the rest of the product names it.
      expect(screen.getByText('Last decided: Active · shared budget 31000402')).toBeDefined();
      expect(screen.getByText('The amount is set on shared budget 31000402')).toBeDefined();
      expect(screen.queryByText(/budget id/)).toBeNull();
      expect(screen.queryByRole('spinbutton')).toBeNull();
      expect(statusSelect()).toHaveProperty('value', 'paused');

      fireEvent.click(saveButton());
      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.searchPartners },
        patch: { status: 'paused' },
      });
    });

    it('names the shared budget by internal id when the API could not name it', async () => {
      mockApi({
        [logKey(SHARED_MEMBER)]: json(200, [
          makeDecision('shared_member', { budget_external_id: null }),
        ]),
      });
      await renderForm({ subject: SHARED_MEMBER });

      // budget_id 402 is FIXTURE_BUDGET_IDS.search, which the shape establishes is shared.
      expect(screen.getByText('Last decided: Active · shared budget id 402')).toBeDefined();
      expect(screen.getByText('The amount is set on shared budget id 402')).toBeDefined();
      expect(screen.queryByRole('spinbutton')).toBeNull();
    });

    it('says the amount is on its shared budget when the assignment is unknown', async () => {
      mockApi({
        [logKey(SHARED_MEMBER)]: json(200, [
          makeDecision('shared_member', { budget_id: null, budget_external_id: null }),
        ]),
      });
      await renderForm({ subject: SHARED_MEMBER });

      expect(screen.getByText('The amount is set on its shared budget')).toBeDefined();
      expect(screen.getByText('Last decided: Active')).toBeDefined();
    });

    it('offers a DAILY shared budget its daily budget and no status, listing members', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(SHARED_BUDGET)]: json(200, [makeDecision('budget')]),
        'POST /decisions': json(
          201,
          makeDecision('budget', { id: 903, daily_budget: '600.00', origin: 'recommendation' }),
        ),
      });
      await renderForm({
        subject: SHARED_BUDGET,
        prefill: { status: 'paused', daily_budget: '600' },
        recommendationId: FIXTURE_ITEM_IDS.b3,
        members: [
          {
            id: String(FIXTURE_CAMPAIGN_IDS.displayProspecting),
            name: 'Display Prospecting — US',
          },
          { id: '0', name: '' },
          {
            id: String(FIXTURE_CAMPAIGN_IDS.displayRetargeting),
            name: 'Display Retargeting — US',
          },
        ],
        onDone,
      });

      expect(screen.getByText('Last decided: daily budget USD 500.00')).toBeDefined();
      expect(
        screen.getByText(
          'Applies to the shared budget used by: Display Prospecting — US, Display Retargeting — US',
        ),
      ).toBeDefined();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(screen.queryByRole('spinbutton', { name: 'Total budget' })).toBeNull();
      // The proposed amount is shown padded to the currency's minor unit.
      expect(amountInput('Daily budget')).toHaveProperty('value', '600.00');

      fireEvent.click(saveButton());
      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
        subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
        patch: { daily_budget: '600.00' },
        recommendation_id: FIXTURE_ITEM_IDS.b3,
      });
    });

    it('offers a CUSTOM_PERIOD shared budget its total budget only', async () => {
      mockApi({
        [logKey(SHARED_BUDGET)]: json(200, [
          makeDecision('budget', { daily_budget: null, total_budget: '9000.00' }),
        ]),
      });
      await renderForm({ subject: SHARED_BUDGET, prefill: { daily_budget: '700.00' } });

      expect(amountInput('Total budget')).toHaveProperty('value', '9000.00');
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(saveButton()).toHaveProperty('disabled', true);
    });

    it('prefers the currency it is given, and names none when no currency is known', async () => {
      const unpriced = makeDecision('own_budget');
      unpriced.subject.currency = null;
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [unpriced]) });
      const { unmount } = await renderForm({ subject: OWN_BUDGET });
      expect(screen.getByText('In the account currency')).toBeDefined();
      unmount();

      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, currency: 'EUR' });
      expect(screen.getByText('In EUR')).toBeDefined();
      expect(screen.getByText('Last decided: Active · daily budget EUR 200.00')).toBeDefined();
    });
  });

  describe('prefill and amounts as the API sends them', () => {
    it('starts a C1 decision at the proposed paused status, which is a change', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': json(
          201,
          makeDecision('own_budget', {
            id: 905,
            status: 'paused',
            origin: 'recommendation',
            recommendation_id: FIXTURE_ITEM_IDS.c1,
            patch: { status: 'paused' },
          }),
        ),
      });
      await renderForm({
        subject: OWN_BUDGET,
        recommendationId: FIXTURE_ITEM_IDS.c1,
        prefill: { status: 'paused' },
        onDone,
      });

      expect(statusSelect()).toHaveProperty('value', 'paused');
      expect(amountInput('Daily budget')).toHaveProperty('value', '200.00');
      expect(saveButton()).toHaveProperty('disabled', false);
      expect(screen.queryByText('No change from the last decided state')).toBeNull();

      // Submitted without any edit: the prefill alone is the patch, tied to its item.
      fireEvent.click(saveButton());
      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
        patch: { status: 'paused' },
        recommendation_id: FIXTURE_ITEM_IDS.c1,
      });
    });

    it('treats a prefill equal to the decided state as no change', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'active', daily_budget: '200' } });

      expect(amountInput('Daily budget')).toHaveProperty('value', '200.00');
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.getByText('No change from the last decided state')).toBeDefined();
    });

    it('shows a Google micro amount unrounded and pads a LinkedIn amount to cents', async () => {
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [
          makeDecision('own_budget', { daily_budget: '210.000001' }),
        ]),
      });
      const { unmount } = await renderForm({ subject: OWN_BUDGET });
      expect(amountInput('Daily budget')).toHaveProperty('value', '210.000001');
      expect(saveButton()).toHaveProperty('disabled', true);
      unmount();

      mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [
          linkedinDecision({ daily_budget: '100.000000', total_budget: null }),
        ]),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN });
      expect(amountInput('Daily budget')).toHaveProperty('value', '100.00');
    });

    it('starts a subject with no decision yet from its discrepancy', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, []),
        'POST /decisions': json(201, makeDecision('own_budget', { status: 'paused' })),
      });
      await renderForm({
        subject: OWN_BUDGET,
        discrepancy: makeDiscrepancy('untracked'),
        onDone,
      });

      expect(
        screen.getByText('No decision recorded yet · platform shows Active · daily budget USD 200.00'),
      ).toBeDefined();
      expect(screen.getByText('In USD')).toBeDefined();
      expect(saveButton()).toHaveProperty('disabled', true);

      typeInto(statusSelect(), 'paused');
      fireEvent.click(saveButton());
      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
        patch: { status: 'paused' },
      });
    });
  });

  describe('amount validation (AAP 0.5.2: minor unit, whole micros)', () => {
    it.each([
      ['', 'Enter an amount'],
      ['-5', 'Enter a non-negative amount'],
      ['10.005', 'Use at most 2 decimal places'],
      ['0.00', 'Enter an amount above zero'],
    ])('refuses %j on a EUR LinkedIn campaign: %s', async (value, message) => {
      mockApi({ [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]) });
      await renderForm({ subject: LINKEDIN_CAMPAIGN });

      typeInto(amountInput('Daily budget'), value);

      expect(screen.getByText(message)).toBeDefined();
      expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBe('true');
      expect(saveButton()).toHaveProperty('disabled', true);
      // An invalid amount is an error to fix, not "no change".
      expect(screen.queryByText('No change from the last decided state')).toBeNull();
    });

    it('accepts trailing zeroes beyond the minor unit and posts the amount as typed', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]),
        'POST /decisions': json(201, linkedinDecision({ daily_budget: '12.34' })),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN, onDone });

      typeInto(amountInput('Daily budget'), '12.3400');
      expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { daily_budget: '12.3400' },
      });
    });

    it('refuses any decimal place on a zero-decimal currency', async () => {
      mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [
          linkedinDecision({
            subject: linkedinSubject('JPY'),
            daily_budget: '1500',
            total_budget: null,
          }),
        ]),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN });

      expect(amountInput('Daily budget')).toHaveProperty('value', '1500');
      expect(amountInput('Daily budget').getAttribute('step')).toBe('1');
      typeInto(amountInput('Daily budget'), '1.5');
      expect(screen.getByText('Use a whole amount, with no decimal places')).toBeDefined();
      expect(saveButton()).toHaveProperty('disabled', true);
    });

    // The places are the ISO 4217 minor unit the API validates against (`minor_unit_exponent`
    // in the backend's decisions.py), never CLDR's: IQD has 3 and AFN 2, where CLDR gives 0.
    it.each([
      ['USD', '50.00', '0.01', '50.001', 'Use at most 2 decimal places', '50.01'],
      ['JPY', '50', '1', '50.5', 'Use a whole amount, with no decimal places', '51'],
      ['KWD', '50.000', '0.001', '50.0001', 'Use at most 3 decimal places', '50.001'],
      ['IQD', '50.000', '0.001', '50.0001', 'Use at most 3 decimal places', '50.001'],
      ['AFN', '50.00', '0.01', '50.001', 'Use at most 2 decimal places', '50.01'],
    ])(
      'steps, shows and validates a %s LinkedIn amount at its ISO minor unit',
      async (currency, shown, step, finer, message, accepted) => {
        mockApi({
          [logKey(LINKEDIN_CAMPAIGN)]: json(200, [
            linkedinDecision({
              subject: linkedinSubject(currency),
              daily_budget: '50',
              total_budget: null,
            }),
          ]),
        });
        await renderForm({ subject: LINKEDIN_CAMPAIGN });

        expect(
          screen.getByText(`Last decided: Active · daily budget ${currency} ${shown}`),
        ).toBeDefined();
        expect(amountInput('Daily budget')).toHaveProperty('value', shown);
        expect(amountInput('Daily budget').getAttribute('step')).toBe(step);

        typeInto(amountInput('Daily budget'), finer);
        expect(screen.getByText(message)).toBeDefined();
        expect(saveButton()).toHaveProperty('disabled', true);

        typeInto(amountInput('Daily budget'), accepted);
        expect(screen.queryByText(message)).toBeNull();
        expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
        expect(saveButton()).toHaveProperty('disabled', false);
      },
    );

    it('accepts whole micros on Google and refuses anything finer', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });

      typeInto(amountInput('Daily budget'), '210.000001');
      expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
      expect(saveButton()).toHaveProperty('disabled', false);

      typeInto(amountInput('Daily budget'), '210.0000001');
      expect(screen.getByText('Use at most 6 decimal places')).toBeDefined();
      expect(saveButton()).toHaveProperty('disabled', true);
    });

    it('refuses a note longer than 2,000 characters', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' } });

      typeInto(noteField(), 'x'.repeat(2001));
      expect(screen.getByText('Use at most 2,000 characters')).toBeDefined();
      expect(saveButton()).toHaveProperty('disabled', true);

      typeInto(noteField(), 'x'.repeat(2000));
      expect(screen.queryByText('Use at most 2,000 characters')).toBeNull();
      expect(saveButton()).toHaveProperty('disabled', false);
    });
  });

  describe('announcing validation errors (WCAG 2.2 SC 4.1.3, 3.3.1)', () => {
    it('renders one empty polite status region from the first render', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });

      const region = formStatus();
      expect(formOf(saveButton()).contains(region)).toBe(true);
      expect(region.id).not.toBe('');
      // No child node at all, not even an empty text node, so the Stack adds no gap for it.
      expect(region.childNodes).toHaveLength(0);
      // Feature code styles nothing itself (AAP 0.4.4): the region is a plain, visible div.
      expect(region.getAttribute('class')).toBeNull();
      expect(region.getAttribute('style')).toBeNull();
      // The unchanged form's button is described by the no-change hint, not by the region.
      const hint = screen.getByText('No change from the last decided state');
      expect(saveButton().getAttribute('aria-describedby')).toBe(hint.id);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it.each([
      ['', 'Enter an amount'],
      ['-5', 'Enter a non-negative amount'],
      ['0', 'Enter an amount above zero'],
    ])('announces %j in the daily budget as "%s"', async (value, message) => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });
      expect(amountInput('Daily budget')).toHaveProperty('value', '200.00');
      const region = formStatus();

      typeInto(amountInput('Daily budget'), value);

      // The same region receives the summary, so it existed before its message.
      expect(formStatus()).toBe(region);
      expect(region.textContent).toBe(`1 error: Daily budget: ${message}.`);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(saveButton().getAttribute('aria-describedby')).toBe(region.id);
      expect(screen.queryByText('No change from the last decided state')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('counts every invalid field, in display order', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });

      typeInto(noteField(), 'x'.repeat(2001));
      typeInto(amountInput('Daily budget'), '');

      expect(formStatus().textContent).toBe(
        '2 errors: Daily budget: Enter an amount. Note: Use at most 2,000 characters.',
      );
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(saveButton().getAttribute('aria-describedby')).toBe(formStatus().id);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('names a missing intended status first, then the amounts', async () => {
      const discrepancy: Discrepancy = makeDiscrepancy('untracked', {
        platform_value: {
          status: null,
          daily_budget: '200.00',
          total_budget: null,
          currency: 'USD',
        },
      });
      mockApi({ [logKey(OWN_BUDGET)]: json(200, []) });
      await renderForm({ subject: OWN_BUDGET, mode: 'intended_state', discrepancy });

      expect(formStatus().textContent).toBe('1 error: Status: Choose a status.');
      typeInto(amountInput('Daily budget'), '');
      expect(formStatus().textContent).toBe(
        '2 errors: Status: Choose a status. Daily budget: Enter an amount.',
      );
      expect(saveButton('Save intended state').getAttribute('aria-describedby')).toBe(
        formStatus().id,
      );
    });

    it('empties the region once the value is fixed', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });
      const region = formStatus();

      typeInto(amountInput('Daily budget'), '-5');
      expect(region.textContent).toBe('1 error: Daily budget: Enter a non-negative amount.');

      typeInto(amountInput('Daily budget'), '150.00');
      expect(formStatus()).toBe(region);
      expect(region.childNodes).toHaveLength(0);
      expect(saveButton()).toHaveProperty('disabled', false);
      expect(saveButton().getAttribute('aria-describedby')).toBeNull();
    });

    it('announces no error on a locked form', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, prefill: { daily_budget: '0' }, disabled: true });

      expect(screen.getByText('Enter an amount above zero')).toBeDefined();
      expect(formStatus().childNodes).toHaveLength(0);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(saveButton().getAttribute('aria-describedby')).toBeNull();
    });

    it('puts the success text in the same single region', async () => {
      const onDone = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': json(201, makeDecision('own_budget', { daily_budget: '150.00' })),
      });
      await renderForm({ subject: OWN_BUDGET, onDone });
      const region = formStatus();

      typeInto(amountInput('Daily budget'), '');
      expect(region.textContent).toBe('1 error: Daily budget: Enter an amount.');
      typeInto(amountInput('Daily budget'), '150.00');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      await waitFor(() => {
        expect(region.textContent).toBe('Decision saved');
      });
      expect(formStatus()).toBe(region);
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  describe('submitting a decision (POST /decisions)', () => {
    it('refuses an unchanged or no-change patch without a request', async () => {
      const api = mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET });

      // The same amount written differently, and a note alone, change nothing decided.
      typeInto(amountInput('Daily budget'), '200');
      expect(saveButton()).toHaveProperty('disabled', true);
      typeInto(amountInput('Daily budget'), '200.0');
      typeInto(noteField(), 'Checked, nothing to change');
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.getByText('No change from the last decided state')).toBeDefined();

      fireEvent.submit(formOf(saveButton()));
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
      expect(api.calls).toHaveLength(1);
    });

    it('posts a typed daily budget alone, keeping the decided status out of the patch', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': json(
          201,
          makeDecision('own_budget', {
            id: 906,
            origin: 'user',
            decided_by: 'admin@example.com',
            daily_budget: '150.00',
            patch: { daily_budget: '150.00' },
          }),
        ),
      });
      await renderForm({ subject: OWN_BUDGET, onDone });

      typeInto(amountInput('Daily budget'), '150.00');
      expect(statusSelect()).toHaveProperty('value', 'active');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
        patch: { daily_budget: '150.00' },
      });
      // The server keeps the decided status; the form never resends an unchanged field.
      expect(post.body).not.toHaveProperty('patch.status');
    });

    it('posts only changed fields with the CSRF header, then calls onDone', async () => {
      const onDone = vi.fn<() => void>();
      const queryClient = createTestQueryClient();
      const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
      const saved = makeDecision('own_budget', {
        id: 904,
        origin: 'recommendation',
        decided_by: 'admin@example.com',
        daily_budget: '240.00',
        recommendation_id: FIXTURE_ITEM_IDS.b1,
        patch: { daily_budget: '240.00' },
        note: 'Sustained cap hits',
      });
      const pending = deferredResponse();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': () => pending.promise,
      });
      await renderForm(
        {
          subject: OWN_BUDGET,
          recommendationId: FIXTURE_ITEM_IDS.b1,
          prefill: { daily_budget: '240' },
          onDone,
        },
        { queryClient },
      );
      expect(amountInput('Daily budget')).toHaveProperty('value', '240.00');

      typeInto(noteField(), '  Sustained cap hits  ');
      // The refetch after the save reads the new decision.
      api.use({ [logKey(OWN_BUDGET)]: json(200, [saved, makeDecision('own_budget')]) });
      fireEvent.click(saveButton());

      // In flight: the button is busy and cannot be pressed again. It is aria-disabled, never
      // natively disabled, so it would keep keyboard focus.
      await waitFor(() => {
        expect(saveButton().getAttribute('aria-busy')).toBe('true');
      });
      expect(saveButton().getAttribute('aria-disabled')).toBe('true');
      expect(saveButton()).toHaveProperty('disabled', false);
      fireEvent.click(saveButton());
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
        patch: { daily_budget: '240.00' },
        recommendation_id: FIXTURE_ITEM_IDS.b1,
        note: 'Sustained cap hits',
      });

      pending.resolve(json(201, saved));
      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(screen.getByRole('status').textContent).toBe('Decision saved');
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.recommendationsAll });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.discrepanciesAll });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.decisionsAll });

      // The refreshed log is the new last decided state, so the form shows no change again.
      expect(
        await screen.findByText('Last decided: Active · daily budget USD 240.00'),
      ).toBeDefined();
      expect(callsTo(api, 'GET', '/decisions')).toHaveLength(2);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it("shows a 422 as the API's message and moves focus to it", async () => {
      const onDone = vi.fn<() => void>();
      const message = 'daily_budget 200.005 is finer than the minor unit of USD.';
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(422, 'sub_minor_unit', message),
      });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, onDone });

      fireEvent.click(saveButton());

      expect(await screen.findByText(message)).toBeDefined();
      await waitFor(() => {
        expect(document.activeElement?.textContent).toBe(message);
      });
      expect(screen.queryByRole('alert')).toBeNull();
      expect(onDone).not.toHaveBeenCalled();
      // A validation error leaves the form usable, so the patch can be corrected and resent.
      expect(saveButton()).toHaveProperty('disabled', false);
      expect(statusSelect()).toHaveProperty('disabled', false);
    });

    it('locks every control on a 409 subject_retired', async () => {
      const onDone = vi.fn<() => void>();
      const onCancel = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(409, 'subject_retired', 'The dataset is no longer active.'),
      });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, onDone, onCancel });

      fireEvent.click(saveButton());

      expect(await screen.findByText('Computed from replaced data')).toBeDefined();
      expect(screen.queryByText('The dataset is no longer active.')).toBeNull();
      expect(statusSelect()).toHaveProperty('disabled', true);
      expect(amountInput('Daily budget')).toHaveProperty('disabled', true);
      expect(noteField()).toHaveProperty('disabled', true);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true);
      expect(screen.queryByRole('alert')).toBeNull();
      expect(onDone).not.toHaveBeenCalled();
    });

    it('calls onCancel from its Cancel button', async () => {
      const onCancel = vi.fn<() => void>();
      const api = mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, onCancel });

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

      expect(onCancel).toHaveBeenCalledTimes(1);
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });

    it('disables every field and the submit button when disabled', async () => {
      const api = mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, disabled: true });

      expect(statusSelect()).toHaveProperty('disabled', true);
      expect(amountInput('Daily budget')).toHaveProperty('disabled', true);
      expect(noteField()).toHaveProperty('disabled', true);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.queryByText('No change from the last decided state')).toBeNull();

      fireEvent.submit(formOf(saveButton()));
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });
  });

  describe('submitting a total budget (AAP 0.5.2 lifetime and CUSTOM_PERIOD budgets)', () => {
    it('posts a CUSTOM_PERIOD shared budget only its total, never status or daily', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(SHARED_BUDGET)]: json(200, [
          makeDecision('budget', { daily_budget: null, total_budget: '9000.00' }),
        ]),
        'POST /decisions': json(
          201,
          makeDecision('budget', {
            id: 907,
            origin: 'user',
            decided_by: 'admin@example.com',
            daily_budget: null,
            total_budget: '9500.00',
            patch: { total_budget: '9500.00' },
          }),
        ),
      });
      await renderForm({
        subject: SHARED_BUDGET,
        // Neither field is in a CUSTOM_PERIOD budget's shape, so neither may reach the patch.
        prefill: { status: 'paused', daily_budget: '700.00' },
        members: [
          {
            id: String(FIXTURE_CAMPAIGN_IDS.displayProspecting),
            name: 'Display Prospecting — US',
          },
        ],
        onDone,
      });

      expect(screen.getByText('Last decided: total budget USD 9,000.00')).toBeDefined();
      expect(
        screen.getByText('Applies to the shared budget used by: Display Prospecting — US'),
      ).toBeDefined();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      typeInto(amountInput('Total budget'), '9500.00');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
        patch: { total_budget: '9500.00' },
      });
      expect(post.body).not.toHaveProperty('patch.status');
      expect(post.body).not.toHaveProperty('patch.daily_budget');
      expect(postsOf(api)).toEqual([post]);
    });

    it('posts a lifetime LinkedIn campaign its total alone, keeping status out', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision({ daily_budget: null })]),
        'POST /decisions': json(
          201,
          linkedinDecision({
            id: 908,
            origin: 'user',
            decided_by: 'admin@example.com',
            daily_budget: null,
            total_budget: '3600.00',
            patch: { total_budget: '3600.00' },
          }),
        ),
      });
      // A lifetime campaign has no daily budget, so a proposed one is dropped, never posted.
      await renderForm({ subject: LINKEDIN_CAMPAIGN, prefill: { daily_budget: '240.00' }, onDone });

      expect(screen.getByText('Last decided: Active · total budget EUR 3,000.00')).toBeDefined();
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      expect(amountInput('Total budget').getAttribute('step')).toBe('0.01');
      typeInto(amountInput('Total budget'), '3600.00');
      expect(statusSelect()).toHaveProperty('value', 'active');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { total_budget: '3600.00' },
      });
      expect(post.body).not.toHaveProperty('patch.status');
      expect(post.body).not.toHaveProperty('patch.daily_budget');
      expect(postsOf(api)).toEqual([post]);
    });

    it('posts a Google CUSTOM_PERIOD campaign its total alone, in whole micros', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, [
          makeDecision('own_budget', { daily_budget: null, total_budget: '6000.00' }),
        ]),
        'POST /decisions': json(
          201,
          makeDecision('own_budget', {
            id: 909,
            origin: 'user',
            decided_by: 'admin@example.com',
            daily_budget: null,
            total_budget: '6000.000001',
            patch: { total_budget: '6000.000001' },
          }),
        ),
      });
      await renderForm({ subject: OWN_BUDGET, onDone });

      expect(screen.getByText('Last decided: Active · total budget USD 6,000.00')).toBeDefined();
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      const total = amountInput('Total budget');
      expect(total).toHaveProperty('value', '6000.00');
      // Google amounts may carry whole micros, whatever the currency, and nothing finer.
      expect(total.getAttribute('step')).toBe('0.000001');
      typeInto(total, '6000.0000001');
      expect(screen.getByText('Use at most 6 decimal places')).toBeDefined();
      expect(saveButton()).toHaveProperty('disabled', true);
      typeInto(total, '6000.000001');
      expect(total.getAttribute('aria-invalid')).toBeNull();
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
        patch: { total_budget: '6000.000001' },
      });
      expect(post.body).not.toHaveProperty('patch.status');
      expect(post.body).not.toHaveProperty('patch.daily_budget');
      expect(postsOf(api)).toEqual([post]);
    });

    it('posts only the total of a dual-budget campaign when only the total changed', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]),
        'POST /decisions': json(
          201,
          linkedinDecision({
            id: 910,
            origin: 'user',
            decided_by: 'admin@example.com',
            total_budget: '3500.00',
            patch: { total_budget: '3500.00' },
          }),
        ),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN, onDone });

      typeInto(amountInput('Total budget'), '3500.00');
      expect(amountInput('Daily budget')).toHaveProperty('value', '100.00');
      expect(statusSelect()).toHaveProperty('value', 'active');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { total_budget: '3500.00' },
      });
      // The server keeps the decided status and daily budget; unchanged fields are never resent.
      expect(post.body).not.toHaveProperty('patch.status');
      expect(post.body).not.toHaveProperty('patch.daily_budget');
      expect(postsOf(api)).toEqual([post]);
    });

    it('posts the status and both budgets of a dual-budget campaign when all changed', async () => {
      const onDone = vi.fn<() => void>();
      const patch = { status: 'paused', daily_budget: '120.00', total_budget: '3600.00' } as const;
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]),
        'POST /decisions': json(
          201,
          linkedinDecision({
            id: 911,
            origin: 'user',
            decided_by: 'admin@example.com',
            ...patch,
            patch,
            note: 'New quarter plan',
          }),
        ),
      });
      await renderForm({ subject: LINKEDIN_CAMPAIGN, onDone });

      typeInto(statusSelect(), 'paused');
      typeInto(amountInput('Daily budget'), '120.00');
      typeInto(amountInput('Total budget'), '3600.00');
      typeInto(noteField(), 'New quarter plan');
      fireEvent.click(saveButton());

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', '/decisions');
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { status: 'paused', daily_budget: '120.00', total_budget: '3600.00' },
        note: 'New quarter plan',
      });
      expect(postsOf(api)).toEqual([post]);
    });

    it('refuses a total budget finer than the minor unit, posting nothing', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({ [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]) });
      await renderForm({ subject: LINKEDIN_CAMPAIGN, onDone });

      // A valid status change does not carry an invalid total through: nothing is saved.
      typeInto(statusSelect(), 'paused');
      typeInto(amountInput('Total budget'), '3000.005');

      expect(screen.getByText('Use at most 2 decimal places')).toBeDefined();
      expect(amountInput('Total budget').getAttribute('aria-invalid')).toBe('true');
      expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.queryByText('No change from the last decided state')).toBeNull();

      fireEvent.submit(formOf(saveButton()));
      expect(postsOf(api)).toHaveLength(0);
      expect(onDone).not.toHaveBeenCalled();
    });
  });

  describe('recording an intended state (AAP 0.5.4.4 record_intended)', () => {
    it('refuses to render without the discrepancy to resolve', async () => {
      mockApi({ [logKey(OWN_BUDGET)]: json(200, []) });
      await renderForm({ subject: OWN_BUDGET, mode: 'intended_state' });

      expect(
        screen.getByText('No discrepancy was given to record an intended state for.'),
      ).toBeDefined();
      expect(screen.queryByRole('button', { name: 'Save intended state' })).toBeNull();
    });

    it('posts the complete own-budget state to the discrepancy', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          makeDiscrepancy('untracked', {
            state: 'awaiting_platform',
            resolution: 'record_intended',
          }),
        ),
      });
      await renderForm({
        subject: OWN_BUDGET,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked'),
        onDone,
      });

      // The complete state may be saved as the platform shows it.
      expect(saveButton('Save intended state')).toHaveProperty('disabled', false);
      expect(screen.queryByText('No change from the last decided state')).toBeNull();
      typeInto(statusSelect(), 'paused');
      typeInto(noteField(), 'Paused on purpose');
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(screen.getByRole('status').textContent).toBe('Intended state saved');
      const post = onlyCall(api, 'POST', RESOLVE_PATH);
      expect(post.params).toEqual({ id: String(FIXTURE_DISCREPANCY_ID) });
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        resolution: 'record_intended',
        intended_state: { status: 'paused', daily_budget: '200.00' },
        note: 'Paused on purpose',
      });
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });

    it('sends a shared budget its one amount and no status', async () => {
      const api = mockApi({
        [logKey(SHARED_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          makeDiscrepancy('untracked', { shape: 'budget', state: 'awaiting_platform' }),
        ),
      });
      const onDone = vi.fn<() => void>();
      await renderForm({
        subject: SHARED_BUDGET,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked', { shape: 'budget' }),
        onDone,
      });

      expect(screen.queryByRole('combobox')).toBeNull();
      typeInto(amountInput('Daily budget'), '450.00');
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', RESOLVE_PATH).body).toEqual({
        resolution: 'record_intended',
        intended_state: { daily_budget: '450.00' },
      });
    });

    it("sends a shared member its status only, naming the budget by Google's id", async () => {
      const api = mockApi({
        [logKey(SHARED_MEMBER)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          makeDiscrepancy('untracked', { shape: 'shared_member', state: 'awaiting_platform' }),
        ),
      });
      const onDone = vi.fn<() => void>();
      await renderForm({
        subject: SHARED_MEMBER,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked', { shape: 'shared_member' }),
        prefill: { daily_budget: '50.00' },
        onDone,
      });

      expect(screen.getByText('The amount is set on shared budget 31000402')).toBeDefined();
      expect(screen.queryByRole('spinbutton')).toBeNull();
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', RESOLVE_PATH).body).toEqual({
        resolution: 'record_intended',
        intended_state: { status: 'active' },
      });
    });

    it('names a budget known only by internal id neutrally and posts status only', async () => {
      // The API fills budget_external_id only for a shared budget the subject knows, so an
      // internal id alone does not establish shared ownership (DiscrepancyResolver words it so).
      const discrepancy = makeDiscrepancy('untracked', {
        shape: 'shared_member',
        platform_value: {
          status: 'active',
          budget_id: FIXTURE_BUDGET_IDS.display,
          budget_external_id: null,
        },
      });
      const api = mockApi({
        [logKey(SHARED_MEMBER)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          makeDiscrepancy('untracked', { shape: 'shared_member', state: 'awaiting_platform' }),
        ),
      });
      const onDone = vi.fn<() => void>();
      await renderForm({ subject: SHARED_MEMBER, mode: 'intended_state', discrepancy, onDone });

      const budgetLabel = `budget (internal id ${String(FIXTURE_BUDGET_IDS.display)})`;
      expect(
        screen.getByText(`No decision recorded yet · platform shows Active · ${budgetLabel}`),
      ).toBeDefined();
      expect(screen.getByText(`The amount is set on ${budgetLabel}`)).toBeDefined();
      expect(screen.queryByText(/shared budget/)).toBeNull();
      expect(screen.queryByRole('spinbutton')).toBeNull();
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      expect(onlyCall(api, 'POST', RESOLVE_PATH).body).toEqual({
        resolution: 'record_intended',
        intended_state: { status: 'active' },
      });
    });

    it('requires a status when the platform reported none, and every amount', async () => {
      const discrepancy: Discrepancy = makeDiscrepancy('untracked', {
        platform_value: {
          status: null,
          daily_budget: '200.00',
          total_budget: null,
          currency: 'USD',
        },
      });
      mockApi({ [logKey(OWN_BUDGET)]: json(200, []) });
      await renderForm({ subject: OWN_BUDGET, mode: 'intended_state', discrepancy });

      expect(
        screen.getByText(
          'No decision recorded yet · platform shows status not reported · daily budget USD 200.00',
        ),
      ).toBeDefined();
      expect(statusSelect()).toHaveProperty('value', '');
      expect(statusOptions()).toEqual(['Choose a status', 'Active', 'Paused', 'Ended']);
      expect(statusSelect().getAttribute('aria-invalid')).toBe('true');
      expect(saveButton('Save intended state')).toHaveProperty('disabled', true);

      typeInto(statusSelect(), 'active');
      expect(statusSelect().getAttribute('aria-invalid')).toBeNull();
      expect(statusOptions()).toEqual(['Active', 'Paused', 'Ended']);
      expect(saveButton('Save intended state')).toHaveProperty('disabled', false);

      // An intended state is complete: an unchanged amount is still validated.
      typeInto(amountInput('Daily budget'), '');
      expect(screen.getByText('Enter an amount')).toBeDefined();
      expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
    });

    it('reads a 409 discrepancy_resolved as "Already resolved" and stops submitting', async () => {
      const onDone = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': apiError(
          409,
          'discrepancy_resolved',
          'The discrepancy is already resolved.',
        ),
      });
      await renderForm({
        subject: OWN_BUDGET,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked'),
        onDone,
      });

      fireEvent.click(saveButton('Save intended state'));

      expect(await screen.findByText('Already resolved')).toBeDefined();
      await waitFor(() => {
        expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
      });
      expect(onDone).not.toHaveBeenCalled();
    });
  });

  describe('recording an intended total budget (AAP 0.5.4.4 record_intended)', () => {
    it('sends a lifetime campaign its status and total, with no daily budget', async () => {
      const onDone = vi.fn<() => void>();
      const lifetime = { daily_budget: null, total_budget: '3000.00' };
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          linkedinUntracked(lifetime, {
            state: 'awaiting_platform',
            resolution: 'record_intended',
          }),
        ),
      });
      await renderForm({
        subject: LINKEDIN_CAMPAIGN,
        mode: 'intended_state',
        discrepancy: linkedinUntracked(lifetime),
        // A lifetime campaign has no daily budget, so a proposed one is dropped, never sent.
        prefill: { daily_budget: '240.00' },
        onDone,
      });

      expect(
        screen.getByText('No decision recorded yet · platform shows Active · total budget EUR 3,000.00'),
      ).toBeDefined();
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      expect(amountInput('Total budget')).toHaveProperty('value', '3000.00');
      typeInto(statusSelect(), 'paused');
      // A total finer than the EUR minor unit cannot be recorded.
      typeInto(amountInput('Total budget'), '2500.005');
      expect(screen.getByText('Use at most 2 decimal places')).toBeDefined();
      expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
      typeInto(amountInput('Total budget'), '2500.00');
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', RESOLVE_PATH);
      expect(post.params).toEqual({ id: String(FIXTURE_DISCREPANCY_ID) });
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        resolution: 'record_intended',
        intended_state: { status: 'paused', total_budget: '2500.00' },
      });
      expect(post.body).not.toHaveProperty('intended_state.daily_budget');
      expect(postsOf(api)).toEqual([post]);
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });

    it('sends a dual-budget campaign its complete state, unchanged amounts included', async () => {
      const onDone = vi.fn<() => void>();
      const api = mockApi({
        [logKey(LINKEDIN_CAMPAIGN)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          linkedinUntracked(
            { daily_budget: '100.00', total_budget: '3000.00' },
            { state: 'awaiting_platform', resolution: 'record_intended' },
          ),
        ),
      });
      await renderForm({
        subject: LINKEDIN_CAMPAIGN,
        mode: 'intended_state',
        discrepancy: linkedinUntracked({ daily_budget: '100.00', total_budget: '3000.00' }),
        onDone,
      });

      expect(amountInput('Daily budget')).toHaveProperty('value', '100.00');
      typeInto(amountInput('Total budget'), '3200.00');
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', RESOLVE_PATH);
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      // record_intended needs the complete state, so the unchanged status and daily budget go too.
      expect(post.body).toEqual({
        resolution: 'record_intended',
        intended_state: { status: 'active', daily_budget: '100.00', total_budget: '3200.00' },
      });
      expect(postsOf(api)).toEqual([post]);
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });

    it('sends a CUSTOM_PERIOD shared budget its total alone, never status or daily', async () => {
      const onDone = vi.fn<() => void>();
      const customPeriod: DiscrepancyValue = {
        daily_budget: null,
        total_budget: '9000.00',
        currency: 'USD',
        amount_micros: 9_000_000_000,
      };
      const api = mockApi({
        [logKey(SHARED_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': json(
          200,
          makeDiscrepancy('untracked', {
            shape: 'budget',
            platform_value: customPeriod,
            state: 'awaiting_platform',
            resolution: 'record_intended',
          }),
        ),
      });
      await renderForm({
        subject: SHARED_BUDGET,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked', {
          shape: 'budget',
          platform_value: customPeriod,
        }),
        // Neither field is in a CUSTOM_PERIOD budget's shape, so neither may be sent.
        prefill: { status: 'paused', daily_budget: '700.00' },
        onDone,
      });

      expect(
        screen.getByText('No decision recorded yet · platform shows total budget USD 9,000.00'),
      ).toBeDefined();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(screen.queryByRole('spinbutton', { name: 'Daily budget' })).toBeNull();
      expect(amountInput('Total budget')).toHaveProperty('value', '9000.00');
      typeInto(amountInput('Total budget'), '9500.00');
      fireEvent.click(saveButton('Save intended state'));

      await waitFor(() => {
        expect(onDone).toHaveBeenCalledTimes(1);
      });
      const post = onlyCall(api, 'POST', RESOLVE_PATH);
      expect(post.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(post.body).toEqual({
        resolution: 'record_intended',
        intended_state: { total_budget: '9500.00' },
      });
      expect(post.body).not.toHaveProperty('intended_state.status');
      expect(post.body).not.toHaveProperty('intended_state.daily_budget');
      expect(postsOf(api)).toEqual([post]);
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(0);
    });
  });

  describe('outcomes reported to the consumer', () => {
    /** Props of an intended-state form for the fixture's untracked discrepancy. */
    const INTENDED: DecisionFormProps = {
      subject: OWN_BUDGET,
      mode: 'intended_state',
      discrepancy: makeDiscrepancy('untracked'),
    };

    it('calls onRetired once on a 409 subject_retired to a decision', async () => {
      const onRetired = vi.fn<() => void>();
      const onDone = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(409, 'subject_retired', 'The dataset is no longer active.'),
      });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, onRetired, onDone });

      fireEvent.click(saveButton());

      expect(await screen.findByText('Computed from replaced data')).toBeDefined();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });
      expect(onDone).not.toHaveBeenCalled();
    });

    it('calls onRetired once on a 409 subject_retired to an intended state', async () => {
      const onRetired = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': apiError(
          409,
          'subject_retired',
          'The dataset is no longer active.',
        ),
      });
      await renderForm({ ...INTENDED, onRetired });

      fireEvent.click(saveButton('Save intended state'));

      expect(await screen.findByText('Computed from replaced data')).toBeDefined();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });
      expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
    });

    it('does not call onRetired on a 422 or a 409 discrepancy_resolved', async () => {
      const onRetired = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(422, 'no_change', 'The patch changes nothing.'),
      });
      const { unmount } = await renderForm({
        subject: OWN_BUDGET,
        prefill: { status: 'paused' },
        onRetired,
      });
      fireEvent.click(saveButton());
      expect(await screen.findByText('The patch changes nothing.')).toBeDefined();
      unmount();

      mockApi({
        [logKey(OWN_BUDGET)]: json(200, []),
        'POST /discrepancies/:id/resolve': apiError(
          409,
          'discrepancy_resolved',
          'The discrepancy is already resolved.',
        ),
      });
      await renderForm({ ...INTENDED, onRetired });
      fireEvent.click(saveButton('Save intended state'));
      expect(await screen.findByText('Already resolved')).toBeDefined();

      expect(onRetired).not.toHaveBeenCalled();
    });

    /** One save whose request settles when the test says, and what the consumer is told. */
    interface SaveCase {
      /** How the case is named in the test title. */
      name: string;
      /** The form's props, without the callbacks the test adds. */
      props: DecisionFormProps;
      /** The subject's decision log. */
      log: Decision[];
      /** The handler key of the save's request. */
      request: 'POST /decisions' | 'POST /discrepancies/:id/resolve';
      /** The submit button. */
      save: 'Save decision' | 'Save intended state';
      /** The response the save settles with. */
      response: MockResponse;
      /** What the consumer is told after the save settled. */
      outcome: string;
    }

    const SAVE_CASES: SaveCase[] = [
      {
        name: 'decision saved',
        props: { subject: OWN_BUDGET, prefill: { status: 'paused' } },
        log: [makeDecision('own_budget')],
        request: 'POST /decisions',
        save: 'Save decision',
        response: json(201, makeDecision('own_budget', { status: 'paused' })),
        outcome: 'done',
      },
      {
        name: 'intended state saved',
        props: INTENDED,
        log: [],
        request: 'POST /discrepancies/:id/resolve',
        save: 'Save intended state',
        response: json(200, { ...makeDiscrepancy('untracked'), state: 'awaiting_platform' }),
        outcome: 'done',
      },
      {
        name: 'decision refused with 422',
        props: { subject: OWN_BUDGET, prefill: { status: 'paused' } },
        log: [makeDecision('own_budget')],
        request: 'POST /decisions',
        save: 'Save decision',
        response: apiError(422, 'no_change', 'The patch changes nothing.'),
        outcome: 'failed: The patch changes nothing.',
      },
      {
        name: 'intended state refused as retired',
        props: INTENDED,
        log: [],
        request: 'POST /discrepancies/:id/resolve',
        save: 'Save intended state',
        response: apiError(409, 'subject_retired', 'The dataset is no longer active.'),
        outcome: 'retired',
      },
      {
        name: 'intended state already resolved',
        props: INTENDED,
        log: [],
        request: 'POST /discrepancies/:id/resolve',
        save: 'Save intended state',
        response: apiError(409, 'discrepancy_resolved', 'The discrepancy is already resolved.'),
        outcome: 'failed: Already resolved',
      },
    ];

    it.each(SAVE_CASES)(
      'reports a save ($name) as pending until it settles, then its outcome',
      async ({ props, log, request, save, response, outcome }) => {
        const events: string[] = [];
        const pending = deferredResponse();
        mockApi({ [logKey(OWN_BUDGET)]: json(200, log), [request]: () => pending.promise });
        await renderForm({
          ...props,
          onPendingChange: (saving) => {
            events.push(saving ? 'pending' : 'settled');
          },
          onDone: () => {
            events.push('done');
          },
          onRetired: () => {
            events.push('retired');
          },
          onFailed: (message) => {
            events.push(`failed: ${message}`);
          },
        });
        // Rendering the form reports nothing; sending the save reports it pending at once.
        expect(events).toEqual([]);

        fireEvent.click(saveButton(save));
        expect(events).toEqual(['pending']);

        pending.resolve(response);

        await waitFor(() => {
          expect(events).toEqual(['pending', 'settled', outcome]);
        });
      },
    );

    it('passes onFailed the shown message and the error, never a subject_retired', async () => {
      const onFailed = vi.fn<(message: string, error: unknown) => void>();
      const onRetired = vi.fn<() => void>();
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(422, 'invalid_patch', 'A daily budget needs whole cents.'),
      });
      const { unmount } = await renderForm({
        subject: OWN_BUDGET,
        prefill: { status: 'paused' },
        onFailed,
        onRetired,
      });
      fireEvent.click(saveButton());

      expect(await screen.findByText('A daily budget needs whole cents.')).toBeDefined();
      await waitFor(() => {
        expect(onFailed).toHaveBeenCalledTimes(1);
      });
      expect(onFailed).toHaveBeenCalledWith(
        'A daily budget needs whole cents.',
        expect.objectContaining({ status: 422, code: 'invalid_patch' }),
      );
      expect(onRetired).not.toHaveBeenCalled();
      unmount();

      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': apiError(409, 'subject_retired', 'The dataset is no longer active.'),
      });
      await renderForm({ subject: OWN_BUDGET, prefill: { status: 'paused' }, onFailed, onRetired });
      fireEvent.click(saveButton());

      expect(await screen.findByText('Computed from replaced data')).toBeDefined();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });
      expect(onFailed).toHaveBeenCalledTimes(1);
    });

    it('shows and reports the generic text, never the message, of a non-ApiError failure', async () => {
      // Assembled at run time, so the secret scan never meets a literal secret assignment.
      const secret = `${['SESSION', 'SECRET'].join('_')}=${'9f3c'.repeat(8)}`;
      const internalPath = '/srv/ads_engine/.env';
      const failure = new Error(
        `ENOENT: no such file or directory, open '${internalPath}' (${secret})`,
      );
      const onFailed = vi.fn<(message: string, error: unknown) => void>();
      const onDone = vi.fn<() => void>();
      const queryClient = createTestQueryClient();
      const api = mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        'POST /decisions': json(201, makeDecision('own_budget', { status: 'paused' })),
      });
      await renderForm(
        { subject: OWN_BUDGET, prefill: { status: 'paused' }, onFailed, onDone },
        { queryClient },
      );
      // The POST succeeds, then the refetch `useRecordDecision` awaits rejects with a plain
      // Error, which TanStack Query makes the mutation's error.
      vi.spyOn(queryClient, 'invalidateQueries').mockRejectedValue(failure);

      fireEvent.click(saveButton());

      const banner = await screen.findByText('The save failed.');
      await waitFor(() => {
        expect(document.activeElement?.contains(banner)).toBe(true);
      });
      await waitFor(() => {
        expect(onFailed).toHaveBeenCalledTimes(1);
      });
      expect(onFailed).toHaveBeenCalledWith('The save failed.', failure);
      expect(onDone).not.toHaveBeenCalled();
      expect(callsTo(api, 'POST', '/decisions')).toHaveLength(1);
      expect(document.body.innerHTML).not.toContain(internalPath);
      expect(document.body.innerHTML).not.toContain(secret);
      expect(document.body.innerHTML).not.toContain('ENOENT');
    });

    /** The saved decision a late success answers with, which `onDone` receives. */
    const savedPaused = makeDecision('own_budget', { status: 'paused' });

    it.each([
      ['onDone', json(201, savedPaused), [savedPaused]],
      ['onRetired', apiError(409, 'subject_retired', 'The dataset is no longer active.'), []],
      [
        'onFailed',
        apiError(422, 'no_change', 'The patch changes nothing.'),
        [
          'The patch changes nothing.',
          expect.objectContaining({ status: 422, code: 'no_change' }) as unknown,
        ],
      ],
    ] as const)(
      'calls %s even when the form unmounted while its save ran',
      async (callback, response, args) => {
        const outcomes = {
          onDone: vi.fn<() => void>(),
          onRetired: vi.fn<() => void>(),
          onFailed: vi.fn<(message: string, error: unknown) => void>(),
        };
        const onPendingChange = vi.fn<(pending: boolean) => void>();
        const pending = deferredResponse();
        const api = mockApi({
          [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
          'POST /decisions': () => pending.promise,
        });

        /** The form, with a button that removes it, as closing its dialog does. */
        function Closable() {
          const [shown, setShown] = useState(true);
          return (
            <>
              <Button
                onClick={() => {
                  setShown(false);
                }}
              >
                Close form
              </Button>
              {shown ? (
                <DecisionForm
                  subject={OWN_BUDGET}
                  prefill={{ status: 'paused' }}
                  {...outcomes}
                  onPendingChange={onPendingChange}
                />
              ) : null}
            </>
          );
        }
        await renderWithProviders(<Closable />, { path: '/decisions' });
        fireEvent.click(await screen.findByRole('button', { name: 'Save decision' }));
        await waitFor(() => {
          expect(callsTo(api, 'POST', '/decisions')).toHaveLength(1);
        });
        fireEvent.click(screen.getByRole('button', { name: 'Close form' }));
        expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
        expect(onPendingChange.mock.calls).toEqual([[true]]);

        pending.resolve(response);

        await waitFor(() => {
          expect(outcomes[callback]).toHaveBeenCalledTimes(1);
        });
        expect(outcomes[callback]).toHaveBeenCalledWith(...args);
        for (const [name, spy] of Object.entries(outcomes)) {
          if (name !== callback) {
            expect(spy).not.toHaveBeenCalled();
          }
        }
        // The settled save is reported to the consumer although the form is gone.
        expect(onPendingChange.mock.calls).toEqual([[true], [false]]);
      },
    );
  });

  describe('form identity', () => {
    it('starts a fresh form, without the previous edits, when the subject changes', async () => {
      const other: SubjectIn = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm };
      mockApi({
        [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
        [logKey(other)]: json(200, [
          makeDecision('own_budget', {
            id: 805,
            subject: {
              type: 'campaign',
              id: other.id,
              name: 'Generic CRM — US',
              source: 'google',
              currency: 'USD',
            },
            daily_budget: '150.00',
          }),
        ]),
      });

      /** Renders the form on one subject, with a button that moves it to another. */
      function SubjectSwitch() {
        const [subject, setSubject] = useState<SubjectIn>(OWN_BUDGET);
        return (
          <>
            <Button
              onClick={() => {
                setSubject(other);
              }}
            >
              Switch subject
            </Button>
            <DecisionForm subject={subject} />
          </>
        );
      }
      await renderWithProviders(<SubjectSwitch />, { path: '/decisions' });
      await screen.findByText('Last decided: Active · daily budget USD 200.00');

      typeInto(amountInput('Daily budget'), '260.00');
      expect(saveButton()).toHaveProperty('disabled', false);

      fireEvent.click(screen.getByRole('button', { name: 'Switch subject' }));

      expect(await screen.findByText('Last decided: Active · daily budget USD 150.00')).toBeDefined();
      expect(amountInput('Daily budget')).toHaveProperty('value', '150.00');
      expect(saveButton()).toHaveProperty('disabled', true);
    });
  });
});

describe('DecisionForm: the outcome onDone reports', () => {
  it('passes onDone the saved decision, the POST /decisions response, in decision mode', async () => {
    const onDone = vi.fn<(decision?: Decision) => void>();
    const saved = makeDecision('own_budget', {
      id: 921,
      origin: 'recommendation',
      decided_by: 'admin@example.com',
      status: 'active',
      daily_budget: '160.00',
      patch: { daily_budget: '160.00' },
      recommendation_id: FIXTURE_ITEM_IDS.c1,
    });
    mockApi({
      [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
      'POST /decisions': json(201, saved),
    });
    await renderForm({
      subject: OWN_BUDGET,
      prefill: { daily_budget: '160.00' },
      recommendationId: FIXTURE_ITEM_IDS.c1,
      onDone,
    });

    fireEvent.click(saveButton());

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    // The whole materialised row, shape and expected state included, reaches the consumer.
    expect(onDone).toHaveBeenCalledWith(saved);
  });

  it('passes onDone nothing for an intended state, whose resolution returns no decision', async () => {
    const onDone = vi.fn<(decision?: Decision) => void>();
    mockApi({
      [logKey(OWN_BUDGET)]: json(200, []),
      'POST /discrepancies/:id/resolve': json(
        200,
        makeDiscrepancy('untracked', { state: 'awaiting_platform' }),
      ),
    });
    await renderForm({
      subject: OWN_BUDGET,
      mode: 'intended_state',
      discrepancy: makeDiscrepancy('untracked'),
      onDone,
    });

    fireEvent.click(saveButton('Save intended state'));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(onDone.mock.calls).toEqual([[]]);
  });
});

// The API refuses an amount of 10^12 or more with 422 `invalid_amount` (`_MAX_AMOUNT` in the
// backend's decisions.py, the integer digits numeric(18,6) holds), so the field refuses it first.
describe('DecisionForm: the amount bound of 10^12 (AAP 0.5.2 invalid_amount)', () => {
  const BOUND_MESSAGE = 'Enter an amount below 1,000,000,000,000';

  it.each([
    ['1234567890123', 'a 13-digit amount'],
    ['1000000000000', 'exactly 10^12'],
    ['1000000000000.000000', '10^12 with trailing zeroes'],
    ['0001000000000000', '10^12 behind leading zeroes'],
  ])('refuses %j, %s, on its field and posts nothing', async (value) => {
    const api = mockApi({ [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]) });
    await renderForm({ subject: OWN_BUDGET });
    const region = formStatus();

    typeInto(amountInput('Daily budget'), value);

    expect(screen.getByText(BOUND_MESSAGE)).toBeDefined();
    expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBe('true');
    expect(formStatus()).toBe(region);
    expect(region.textContent).toBe(`1 error: Daily budget: ${BOUND_MESSAGE}.`);
    expect(saveButton()).toHaveProperty('disabled', true);
    expect(saveButton().getAttribute('aria-describedby')).toBe(region.id);
    expect(screen.queryByText('No change from the last decided state')).toBeNull();

    fireEvent.submit(formOf(saveButton()));
    await act(async () => {
      await Promise.resolve();
    });
    expect(postsOf(api)).toHaveLength(0);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('names the size before the precision of an amount that breaks both', async () => {
    mockApi({ [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]) });
    await renderForm({ subject: LINKEDIN_CAMPAIGN });

    typeInto(amountInput('Daily budget'), '1234567890123.456');

    expect(screen.getByText(BOUND_MESSAGE)).toBeDefined();
    expect(screen.queryByText('Use at most 2 decimal places')).toBeNull();
    expect(formStatus().textContent).toBe(`1 error: Daily budget: ${BOUND_MESSAGE}.`);
  });

  it('refuses 10^12 in an intended state too, posting no resolution', async () => {
    const api = mockApi({ [logKey(OWN_BUDGET)]: json(200, []) });
    await renderForm({
      subject: OWN_BUDGET,
      mode: 'intended_state',
      discrepancy: makeDiscrepancy('untracked'),
    });

    typeInto(amountInput('Daily budget'), '1000000000000');

    expect(screen.getByText(BOUND_MESSAGE)).toBeDefined();
    expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBe('true');
    expect(formStatus().textContent).toBe(`1 error: Daily budget: ${BOUND_MESSAGE}.`);
    expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
    fireEvent.submit(formOf(saveButton('Save intended state')));
    await act(async () => {
      await Promise.resolve();
    });
    expect(postsOf(api)).toHaveLength(0);
  });

  it.each([
    ['999999999999.99', '999999999999.99'],
    ['999999999999.999999', '999999999999.999999'],
    ['0999999999999', '0999999999999'],
  ])('accepts %j on Google, just below the bound, and posts it as typed', async (value, posted) => {
    const onDone = vi.fn<() => void>();
    const api = mockApi({
      [logKey(OWN_BUDGET)]: json(200, [makeDecision('own_budget')]),
      'POST /decisions': json(201, makeDecision('own_budget', { daily_budget: value })),
    });
    await renderForm({ subject: OWN_BUDGET, onDone });

    typeInto(amountInput('Daily budget'), value);
    expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
    expect(screen.queryByText(BOUND_MESSAGE)).toBeNull();
    expect(formStatus().childNodes).toHaveLength(0);
    expect(saveButton()).toHaveProperty('disabled', false);
    fireEvent.click(saveButton());

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(onlyCall(api, 'POST', '/decisions').body).toEqual({
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
      patch: { daily_budget: posted },
    });
  });

  it('accepts the largest EUR amount below the bound on LinkedIn', async () => {
    mockApi({ [logKey(LINKEDIN_CAMPAIGN)]: json(200, [linkedinDecision()]) });
    await renderForm({ subject: LINKEDIN_CAMPAIGN });

    typeInto(amountInput('Daily budget'), '999999999999.99');

    expect(amountInput('Daily budget').getAttribute('aria-invalid')).toBeNull();
    expect(saveButton()).toHaveProperty('disabled', false);
  });

  it.each([
    ['Google', OWN_BUDGET, makeDecision('own_budget'), '0.000001', '999999999999.999999'],
    ['EUR LinkedIn', LINKEDIN_CAMPAIGN, linkedinDecision(), '0.01', '999999999999.99'],
    [
      'JPY LinkedIn',
      LINKEDIN_CAMPAIGN,
      linkedinDecision({
        subject: linkedinSubject('JPY'),
        daily_budget: '1500',
        total_budget: null,
      }),
      '1',
      '999999999999',
    ],
    [
      'KWD LinkedIn',
      LINKEDIN_CAMPAIGN,
      linkedinDecision({ subject: linkedinSubject('KWD'), daily_budget: '50', total_budget: null }),
      '0.001',
      '999999999999.999',
    ],
  ] as const)(
    'caps the %s amount input at the largest amount below 10^12 on its step',
    async (_subject, subject, decision, step, max) => {
      mockApi({ [logKey(subject)]: json(200, [decision]) });
      await renderForm({ subject });

      const input = amountInput('Daily budget');
      expect(input.getAttribute('min')).toBe('0');
      expect(input.getAttribute('step')).toBe(step);
      expect(input.getAttribute('max')).toBe(max);
      // The form validates itself, so the browser never blocks a submission on its own.
      expect(formOf(input).noValidate).toBe(true);
    },
  );
});

// AAP 0.5.5: "A 409 subject_retired from a stale screen is shown as the same message" ("Computed
// from replaced data"), with every action disabled (AAP 0.3.2). The subject's log lists active
// datasets only, so once the subject is retired its refetch lists no decision, or, from an API
// that refuses the read, answers 409 subject_retired itself; neither may turn the lock into "No
// decided state is recorded for this subject yet."
describe('DecisionForm: the retired lock outlasts every later read (AAP 0.5.5)', () => {
  const RETIRED = 'Computed from replaced data';
  const NO_STATE = 'No decided state is recorded for this subject yet.';

  /** The 409 the API answers a save or a log read about a subject of a replaced dataset. */
  const retiredError = () =>
    apiError(
      409,
      'subject_retired',
      'Campaign 501 does not belong to an active dataset; decisions apply only to the active ' +
        "dataset's subjects",
    );

  /**
   * The subject's log: the latest decision on the first read, then `later` on every refetch.
   *
   * @param later - What every read after the first answers.
   * @returns The handler and the number of reads it has answered.
   */
  function logThen(later: MockResponse) {
    const reads = { count: 0 };
    const handler = () => {
      reads.count += 1;
      return reads.count === 1 ? json(200, [makeDecision('own_budget')]) : later;
    };
    return { handler, reads };
  }

  /** Whether keyboard focus is on the retired banner or the focus target wrapping it. */
  function focusIsOnRetiredBanner(): boolean {
    const active = document.activeElement;
    return (
      active !== null && active !== document.body && active.contains(screen.getByText(RETIRED))
    );
  }

  it.each([
    // An API that lists only active datasets' decisions: the form gives way to the banner alone.
    ['lists no decision', json(200, []), false],
    // An API that refuses the read: the last read's state stays, locked, under the banner.
    ['is refused with 409 subject_retired', retiredError(), true],
  ])('keeps the lock after a refused save when the refetch %s', async (_label, later, kept) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const log = logThen(later);
      const onRetired = vi.fn<() => void>();
      const onCancel = vi.fn<() => void>();
      mockApi({ [logKey(OWN_BUDGET)]: log.handler, 'POST /decisions': retiredError() });
      await renderForm({
        subject: OWN_BUDGET,
        prefill: { daily_budget: '251.00' },
        onRetired,
        onCancel,
      });

      fireEvent.click(saveButton());
      expect(await screen.findByText(RETIRED)).toBeDefined();
      await waitFor(() => {
        expect(focusIsOnRetiredBanner()).toBe(true);
      });

      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      expect(log.reads.count).toBe(2);

      expect(screen.getByText(RETIRED)).toBeDefined();
      expect(screen.queryByText(NO_STATE)).toBeNull();
      if (kept) {
        expect(saveButton()).toHaveProperty('disabled', true);
        expect(amountInput('Daily budget')).toHaveProperty('disabled', true);
      } else {
        expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
        expect(screen.queryByRole('spinbutton')).toBeNull();
      }
      // Focus the refused save put on the message stays on it, also once the form is replaced
      // (WCAG 2.2 SC 2.4.3).
      await waitFor(() => {
        expect(focusIsOnRetiredBanner()).toBe(true);
      });
      expect(screen.queryByRole('alert')).toBeNull();
      // The refused save reported the retirement; a refused read of it reports nothing more.
      expect(onRetired).toHaveBeenCalledTimes(1);
      expect(onCancel).not.toHaveBeenCalled();

      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      expect(screen.getByText(RETIRED)).toBeDefined();
      expect(screen.queryByText(NO_STATE)).toBeNull();
      expect(onRetired).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the retired banner alone when the first read is refused, and calls onRetired once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const onRetired = vi.fn<() => void>();
      const onFailed = vi.fn<(message: string, error: unknown) => void>();
      const api = mockApi({ [logKey(OWN_BUDGET)]: retiredError() });
      await renderForm({
        subject: OWN_BUDGET,
        prefill: { status: 'paused' },
        recommendationId: FIXTURE_ITEM_IDS.c1,
        onRetired,
        onFailed,
      });

      expect(screen.getByText(RETIRED)).toBeDefined();
      expect(screen.queryByText(NO_STATE)).toBeNull();
      // The API's own wording, which names internals, is never shown.
      expect(screen.queryByText(/does not belong to an active dataset/)).toBeNull();
      expect(screen.queryByRole('button', { name: 'Save decision' })).toBeNull();
      expect(screen.queryByRole('combobox')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });
      expect(onFailed).not.toHaveBeenCalled();

      // The refusal is final: polling stops, and the lock is reported no second time.
      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS * 2));
      expect(callsTo(api, 'GET', '/decisions')).toHaveLength(1);
      expect(screen.getByText(RETIRED)).toBeDefined();
      expect(onRetired).toHaveBeenCalledTimes(1);
      expect(postsOf(api)).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('locks a loaded form when a later read is refused with 409 subject_retired', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const log = logThen(retiredError());
      const onRetired = vi.fn<() => void>();
      const api = mockApi({ [logKey(OWN_BUDGET)]: log.handler });
      await renderForm({
        subject: OWN_BUDGET,
        prefill: { daily_budget: '251.00' },
        onRetired,
        onCancel: () => undefined,
      });
      expect(saveButton()).toHaveProperty('disabled', false);
      expect(screen.queryByText(RETIRED)).toBeNull();

      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      expect(log.reads.count).toBe(2);

      // The last decided state is still shown, under the retired banner, with every control off.
      expect(await screen.findByText(RETIRED)).toBeDefined();
      expect(screen.getByText('Last decided: Active · daily budget USD 200.00')).toBeDefined();
      expect(statusSelect()).toHaveProperty('disabled', true);
      expect(amountInput('Daily budget')).toHaveProperty('disabled', true);
      expect(noteField()).toHaveProperty('disabled', true);
      expect(saveButton()).toHaveProperty('disabled', true);
      expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true);
      expect(screen.queryByText(NO_STATE)).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });

      fireEvent.submit(formOf(saveButton()));
      await act(async () => {
        await Promise.resolve();
      });
      expect(postsOf(api)).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('locks an intended state whose subject read is refused, first or on a later poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const onRetired = vi.fn<() => void>();
      let reads = 0;
      const api = mockApi({
        // An untracked subject has no decision yet, so its log lists none until it is refused.
        [logKey(OWN_BUDGET)]: () => {
          reads += 1;
          return reads === 1 ? json(200, []) : retiredError();
        },
      });
      await renderForm({
        subject: OWN_BUDGET,
        mode: 'intended_state',
        discrepancy: makeDiscrepancy('untracked'),
        onRetired,
      });
      expect(saveButton('Save intended state')).toHaveProperty('disabled', false);

      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      expect(reads).toBe(2);

      // The discrepancy still resolves a state, so the locked fields stay readable under it.
      expect(await screen.findByText(RETIRED)).toBeDefined();
      expect(saveButton('Save intended state')).toHaveProperty('disabled', true);
      expect(statusSelect()).toHaveProperty('disabled', true);
      expect(screen.queryByText(NO_STATE)).toBeNull();
      await waitFor(() => {
        expect(onRetired).toHaveBeenCalledTimes(1);
      });
      fireEvent.submit(formOf(saveButton('Save intended state')));
      await act(async () => {
        await Promise.resolve();
      });
      expect(postsOf(api)).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the retired banner alone for an intended state whose first read is refused', async () => {
    const onRetired = vi.fn<() => void>();
    mockApi({ [logKey(OWN_BUDGET)]: retiredError() });
    await renderForm({
      subject: OWN_BUDGET,
      mode: 'intended_state',
      discrepancy: makeDiscrepancy('untracked'),
      onRetired,
    });

    expect(screen.getByText(RETIRED)).toBeDefined();
    expect(screen.queryByRole('button', { name: 'Save intended state' })).toBeNull();
    expect(screen.queryByText(NO_STATE)).toBeNull();
    await waitFor(() => {
      expect(onRetired).toHaveBeenCalledTimes(1);
    });
  });

  it('starts unlocked for another subject after one subject was retired', async () => {
    const other: SubjectIn = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm };
    const onRetired = vi.fn<() => void>();
    mockApi({
      [logKey(OWN_BUDGET)]: retiredError(),
      [logKey(other)]: json(200, [
        makeDecision('own_budget', {
          id: 805,
          subject: {
            type: 'campaign',
            id: other.id,
            name: 'Generic CRM — US',
            source: 'google',
            currency: 'USD',
          },
          daily_budget: '150.00',
        }),
      ]),
    });

    /** Renders the form on the retired subject, with a button that moves it to another. */
    function SubjectSwitch() {
      const [subject, setSubject] = useState<SubjectIn>(OWN_BUDGET);
      return (
        <>
          <Button
            onClick={() => {
              setSubject(other);
            }}
          >
            Switch subject
          </Button>
          <DecisionForm subject={subject} onRetired={onRetired} />
        </>
      );
    }
    await renderWithProviders(<SubjectSwitch />, { path: '/decisions' });
    expect(await screen.findByText(RETIRED)).toBeDefined();

    fireEvent.click(screen.getByRole('button', { name: 'Switch subject' }));

    expect(await screen.findByText('Last decided: Active · daily budget USD 150.00')).toBeDefined();
    expect(screen.queryByText(RETIRED)).toBeNull();
    expect(statusSelect()).toHaveProperty('disabled', false);
    expect(onRetired).toHaveBeenCalledTimes(1);
  });
});
