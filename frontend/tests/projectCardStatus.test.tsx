import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import type { Project } from "../src/api/workbench";
import { ProjectCardStatus } from "../src/features/Workbench";

const project: Project = {
  id: "synthetic-project", creation_order: 1, name: "合成项目", target_bvid: "BV0000000001", target_page: 1,
  completed: false, send_needs_review: false, send_partial_failure: true, send_queue: null,
  activity: { stage: "send", running: false, active: false },
};

it("shows the completion checkmark after marking a partial failure complete and restores the warning on undo", () => {
  const warning = "部分弹幕发送失败，待核对";
  const render = (completed: boolean) =>
    renderToStaticMarkup(<ProjectCardStatus project={{ ...project, completed }} activity={project.activity} />);

  expect(render(false)).toContain(warning);
  const marked = render(true);
  expect(marked).toContain('aria-label="迁移完成"');
  expect(marked).not.toContain(warning);
  expect(render(false)).not.toContain('aria-label="迁移完成"');
});

it("shows the completion checkmark for an explicitly completed project with a paused send", () => {
  const activity = { ...project.activity, running: true };
  const markup = renderToStaticMarkup(<ProjectCardStatus
    project={{ ...project, completed: true, send_partial_failure: false }} activity={activity} />);
  expect(markup).toContain('aria-label="迁移完成"');
});
