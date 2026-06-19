"""ASCII commit-graph rendering for ``log --graph``.

This is a faithful port of C Git's ``graph.c`` state machine (the column model,
expansion/commit/post-merge/collapsing line generation, and the line-prefixing
driver). Color is omitted — pythongit never emits ANSI — so each column simply
contributes its glyph. The public entry point is :class:`Graph`:

    g = Graph(parents_of)          # parents_of(sha) -> list[str]
    out = g.format_commit(sha, text)   # text = the commit's formatted block

``out`` is the commit's block with graph columns prepended to every line, byte
-for-byte matching ``git log --graph``.
"""
from __future__ import annotations

from typing import Callable

# Graph states (graph.c: enum graph_state).
PADDING, SKIP, PRE_COMMIT, COMMIT, POST_MERGE, COLLAPSING = range(6)


class Graph:
    def __init__(self, parents_of: Callable[[str], list]):
        self._parents_of = parents_of
        self.commit = None
        self.num_parents = 0
        self.expansion_row = 0
        self.state = PADDING
        self.prev_state = PADDING
        self.commit_index = 0
        self.prev_commit_index = 0
        self.merge_layout = 0
        self.edges_added = 0
        self.prev_edges_added = 0
        self.num_columns = 0
        self.num_new_columns = 0
        self.mapping_size = 0
        self.columns: list[str] = []        # column.commit (sha) per column
        self.new_columns: list[str] = []
        self.mapping: list[int] = []
        self.old_mapping: list[int] = []
        self.width = 0

    # --- parent helpers (no history simplification: all parents interesting) --
    def _parents(self, sha):
        return list(self._parents_of(sha))

    # --- column bookkeeping (graph.c) -------------------------------------
    def _update_state(self, s):
        self.prev_state = self.state
        self.state = s

    def _find_new_column_by_commit(self, sha):
        for i in range(self.num_new_columns):
            if self.new_columns[i] == sha:
                return i
        return -1

    def _grow(self, arr, idx, fill):
        while len(arr) <= idx:
            arr.append(fill)

    def _insert_into_new_columns(self, sha, idx):
        i = self._find_new_column_by_commit(sha)
        if i < 0:
            i = self.num_new_columns
            self.num_new_columns += 1
            self._grow(self.new_columns, i, None)
            self.new_columns[i] = sha

        if self.num_parents > 1 and idx > -1 and self.merge_layout == -1:
            dist = idx - i
            shift = (2 * dist - 3) if dist > 1 else 1
            self.merge_layout = 0 if dist > 0 else 1
            self.edges_added = self.num_parents + self.merge_layout - 2
            mapping_idx = self.width + (self.merge_layout - 1) * shift
            self.width += 2 * self.merge_layout
        elif self.edges_added > 0 and i == self.mapping[self.width - 2]:
            mapping_idx = self.width - 2
            self.edges_added = -1
        else:
            mapping_idx = self.width
            self.width += 2

        self._grow(self.mapping, mapping_idx, -1)
        self.mapping[mapping_idx] = i

    def _update_columns(self):
        self.columns = self.new_columns
        self.num_columns = self.num_new_columns
        self.new_columns = []
        self.num_new_columns = 0

        max_new = self.num_columns + self.num_parents
        self.mapping_size = 2 * max_new
        self.mapping = [-1] * self.mapping_size
        self.width = 0
        self.prev_edges_added = self.edges_added
        self.edges_added = 0

        seen_this = False
        is_commit_in_columns = True
        i = 0
        while i <= self.num_columns:
            if i == self.num_columns:
                if seen_this:
                    break
                is_commit_in_columns = False
                col_commit = self.commit
            else:
                col_commit = self.columns[i]

            if col_commit == self.commit:
                seen_this = True
                self.commit_index = i
                self.merge_layout = -1
                for parent in self._parents(self.commit):
                    self._insert_into_new_columns(parent, i)
                if self.num_parents == 0:
                    self.width += 2
            else:
                self._insert_into_new_columns(col_commit, -1)
            i += 1

        while self.mapping_size > 1 and self.mapping[self.mapping_size - 1] < 0:
            self.mapping_size -= 1

    def _num_dashed_parents(self):
        return self.num_parents + self.merge_layout - 3

    def _num_expansion_rows(self):
        return self._num_dashed_parents() * 2

    def _needs_pre_commit_line(self):
        return (self.num_parents >= 3
                and self.commit_index < (self.num_columns - 1)
                and self.expansion_row < self._num_expansion_rows())

    def update(self, sha):
        self.commit = sha
        self.num_parents = len(self._parents(sha))
        self.prev_commit_index = self.commit_index
        self._update_columns()
        self.expansion_row = 0
        if self.state != PADDING:
            self.state = SKIP
        elif self._needs_pre_commit_line():
            self.state = PRE_COMMIT
        else:
            self.state = COMMIT

    def _is_mapping_correct(self):
        for i in range(self.mapping_size):
            target = self.mapping[i]
            if target < 0:
                continue
            if target == (i // 2):
                continue
            return False
        return True

    # --- line generation --------------------------------------------------
    def _pad(self, line):
        if len(line) < self.width:
            line += " " * (self.width - len(line))
        return line

    def _padding_line(self):
        line = ""
        for i in range(self.num_new_columns):
            line += "|" + " "
        return line

    def _skip_line(self):
        line = "..."
        if self._needs_pre_commit_line():
            self._update_state(PRE_COMMIT)
        else:
            self._update_state(COMMIT)
        return line

    def _pre_commit_line(self):
        line = ""
        seen_this = False
        for i in range(self.num_columns):
            col = self.columns[i]
            if col == self.commit:
                seen_this = True
                line += "|" + " " * self.expansion_row
            elif seen_this and self.expansion_row == 0:
                if self.prev_state == POST_MERGE and self.prev_commit_index < i:
                    line += "\\"
                else:
                    line += "|"
            elif seen_this and self.expansion_row > 0:
                line += "\\"
            else:
                line += "|"
            line += " "
        self.expansion_row += 1
        if not self._needs_pre_commit_line():
            self._update_state(COMMIT)
        return line

    def _commit_char(self):
        return "*"

    def _draw_octopus_merge(self, line):
        dashed = self._num_dashed_parents()
        for i in range(dashed):
            # column index two to the right of the merge, per mapping
            j = self.mapping[(self.commit_index + i + 2) * 2]
            line += "-"
            line += "." if i == dashed - 1 else "-"
        return line

    def _commit_line(self):
        line = ""
        seen_this = False
        i = 0
        while i <= self.num_columns:
            if i == self.num_columns:
                if seen_this:
                    break
                col_commit = self.commit
            else:
                col_commit = self.columns[i]

            if col_commit == self.commit:
                seen_this = True
                line += self._commit_char()
                if self.num_parents > 2:
                    line = self._draw_octopus_merge(line)
            elif seen_this and self.edges_added > 1:
                line += "\\"
            elif seen_this and self.edges_added == 1:
                if (self.prev_state == POST_MERGE and self.prev_edges_added > 0
                        and self.prev_commit_index < i):
                    line += "\\"
                else:
                    line += "|"
            elif (self.prev_state == COLLAPSING
                  and 2 * i + 1 < len(self.old_mapping)
                  and self.old_mapping[2 * i + 1] == i
                  and self.mapping[2 * i] < i):
                line += "/"
            else:
                line += "|"
            line += " "
            i += 1

        if self.num_parents > 1:
            self._update_state(POST_MERGE)
        elif self._is_mapping_correct():
            self._update_state(PADDING)
        else:
            self._update_state(COLLAPSING)
        return line

    def _post_merge_line(self):
        MERGE_CHARS = "/|\\"
        line = ""
        seen_this = False
        parents = self._parents(self.commit)
        first_parent = parents[0] if parents else None
        parent_col = None  # column index of first parent (for the '_' run)
        i = 0
        while i <= self.num_columns:
            if i == self.num_columns:
                if seen_this:
                    break
                col_commit = self.commit
            else:
                col_commit = self.columns[i]

            if col_commit == self.commit:
                seen_this = True
                idx = self.merge_layout
                for j in range(self.num_parents):
                    par_column = self._find_new_column_by_commit(parents[j])
                    c = MERGE_CHARS[idx]
                    line += c
                    if idx == 2:
                        if self.edges_added > 0 or j < self.num_parents - 1:
                            line += " "
                    else:
                        idx += 1
                if self.edges_added == 0:
                    line += " "
            elif seen_this:
                line += "\\" if self.edges_added > 0 else "|"
                line += " "
            else:
                line += "|"
                if self.merge_layout != 0 or i != self.commit_index - 1:
                    line += "_" if parent_col is not None else " "

            if col_commit == first_parent:
                parent_col = i
            i += 1

        if self._is_mapping_correct():
            self._update_state(PADDING)
        else:
            self._update_state(COLLAPSING)
        return line

    def _collapsing_line(self):
        self.mapping, self.old_mapping = self.old_mapping, self.mapping
        self.mapping = [-1] * self.mapping_size

        used_horizontal = False
        horizontal_edge = -1
        horizontal_edge_target = -1

        for i in range(self.mapping_size):
            target = self.old_mapping[i]
            if target < 0:
                continue
            if target * 2 == i:
                self.mapping[i] = target
            elif self.mapping[i - 1] < 0:
                self.mapping[i - 1] = target
                if horizontal_edge == -1:
                    horizontal_edge = i
                    horizontal_edge_target = target
                    for j in range(target * 2 + 3, i - 2, 2):
                        self.mapping[j] = target
            elif self.mapping[i - 1] == target:
                pass
            else:
                self.mapping[i - 2] = target
                if horizontal_edge == -1:
                    horizontal_edge_target = target
                    horizontal_edge = i - 1
                    for j in range(target * 2 + 3, i - 2, 2):
                        self.mapping[j] = target

        self.old_mapping = list(self.mapping)
        if self.mapping[self.mapping_size - 1] < 0:
            self.mapping_size -= 1

        line = ""
        for i in range(self.mapping_size):
            target = self.mapping[i]
            if target < 0:
                line += " "
            elif target * 2 == i:
                line += "|"
            elif target == horizontal_edge_target and i != horizontal_edge - 1:
                if i != target * 2 + 3:
                    self.mapping[i] = -1
                used_horizontal = True
                line += "_"
            else:
                if used_horizontal and i < horizontal_edge:
                    self.mapping[i] = -1
                line += "/"

        if self._is_mapping_correct():
            self._update_state(PADDING)
        return line

    def _next_line(self):
        """Return (segment, shown_commit_line)."""
        shown = False
        if self.state == PADDING:
            line = self._padding_line()
        elif self.state == SKIP:
            line = self._skip_line()
        elif self.state == PRE_COMMIT:
            line = self._pre_commit_line()
        elif self.state == COMMIT:
            line = self._commit_line()
            shown = True
        elif self.state == POST_MERGE:
            line = self._post_merge_line()
        elif self.state == COLLAPSING:
            line = self._collapsing_line()
        else:
            line = ""
        return self._pad(line), shown

    def is_commit_finished(self):
        return self.state == PADDING

    def _separator_line(self):
        """The blank line between commits (graph.c:graph_padding_line, COMMIT
        state): one ``|`` per pre-commit column, at the post-update width."""
        line = ""
        for i in range(self.num_columns):
            line += "|"
            if i < len(self.columns) and self.columns[i] == self.commit and self.num_parents > 2:
                line += " " * ((self.num_parents - 2) * 2)
            else:
                line += " "
        return self._pad(line)

    # --- driver -----------------------------------------------------------
    def format_commit(self, sha, text: str, emit_separator: bool = False) -> str:
        """Render ``text`` (this commit's formatted block) with graph columns.

        Mirrors graph_show_commit + graph_show_commit_msg: an optional blank
        separator line, expansion lines, then the ``*`` commit line as the
        prefix of the first text line, graph segments before each later text
        line, and finally any remaining graph lines.
        """
        self.update(sha)
        out: list[str] = []
        if emit_separator:
            out.append(self._separator_line() + "\n")
            self.prev_state = PADDING

        # graph_show_commit: emit lines until (and including) the commit line.
        prefix_first = ""
        while True:
            seg, shown = self._next_line()
            if shown:
                prefix_first = seg
                break
            out.append(seg + "\n")

        # graph_show_strbuf: first text line follows the commit-char segment;
        # each subsequent text line is preceded by one graph segment.
        newline_terminated = text.endswith("\n")
        body = text[:-1] if newline_terminated else text
        lines = body.split("\n")
        for n, ln in enumerate(lines):
            if n == 0:
                out.append(prefix_first + ln + "\n")
            else:
                seg, _ = self._next_line()
                out.append(seg + ln + "\n")

        # graph_show_remainder: flush trailing graph lines (post-merge/collapse).
        if not self.is_commit_finished():
            remainder = []
            while True:
                seg, _ = self._next_line()
                remainder.append(seg)
                if self.is_commit_finished():
                    break
            out.append("\n".join(remainder) + "\n")

        return "".join(out)
