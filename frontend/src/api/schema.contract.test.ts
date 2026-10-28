import { expectTypeOf, test } from "vitest";

import type { components } from "@/api/schema";

type Schemas = components["schemas"];
type ApprovalPart = Schemas["ApprovalPart"];
type ApprovalView = Schemas["ApprovalView"];
type RiskClass = Schemas["RiskClass"];
type EventRecord = Schemas["EventRecord"];

// Pinned by the compiler: the build's `tsc -b` compiles this file, vitest only runs it.
test("approvals carry their kind and the nullable stall fields", () => {
  expectTypeOf<ApprovalPart["kind"]>().toEqualTypeOf<"tool" | "stall">();
  expectTypeOf<ApprovalView["kind"]>().toEqualTypeOf<"tool" | "stall">();
  expectTypeOf<ApprovalView["tool"]>().toEqualTypeOf<string | null>();
  expectTypeOf<ApprovalView["risk_class"]>().toEqualTypeOf<RiskClass | null>();
  expectTypeOf<ApprovalView["step"]>().toEqualTypeOf<number | null>();
  expectTypeOf<ApprovalView["proposal_kind"]>().toEqualTypeOf<string | null>();
  // A field with a pydantic default is emitted optional on the part.
  expectTypeOf<ApprovalPart["proposal_kind"]>().toEqualTypeOf<string | null | undefined>();
  expectTypeOf<ApprovalPart["step"]>().toEqualTypeOf<number | null | undefined>();
});

test("the risk class union includes propose", () => {
  expectTypeOf<"propose">().toExtend<RiskClass>();
});

test("the event types include turn.stalled", () => {
  expectTypeOf<"turn.stalled">().toExtend<EventRecord["type"]>();
});
