"""Info about node/set/members.  For admin tool.
"""

import datetime
import re
import sys

from typing import Dict

import skytools

__all__ = ['MemberInfo', 'NodeInfo', 'QueueInfo']

# node types
ROOT = 'root'
BRANCH = 'branch'
LEAF = 'leaf'


class MemberInfo(object):
    """Info about set member."""
    def __init__(self, row):
        self.name = row['node_name']
        self.location = row['node_location']
        self.dead = row['dead']


def ival2str(iv):
    res = ""
    tmp, secs = divmod(iv.seconds, 60)
    hrs, mins = divmod(tmp, 60)
    if iv.days:
        res += "%dd" % iv.days
    if hrs:
        res += "%dh" % hrs
    if mins:
        res += "%dm" % mins
    res += "%ds" % secs
    return res


class NodeInfo(object):
    """Detailed info about set node."""

    name = None
    type = None
    global_watermark = None
    local_watermark = None
    completed_tick = None
    provider_node = None
    provider_location = None
    consumer_name = None  # ?
    worker_name = None    # ?
    paused = False
    uptodate = True
    combined_queue = None
    combined_type = None
    target_for = None
    last_tick = None
    node_attrs: Dict[str, str] = {}
    service = None

    def __init__(self, queue_name, row, main_worker=True, node_name=None, location=None):
        self.queue_name = queue_name
        self.main_worker = main_worker

        self.parent = None
        self.consumer_map = {}
        self.queue_info = {}
        self._info_lines = []
        self.cascaded_consumer_map = {}

        self._row = row

        if location:
            m = re.search(r'service=(\S+)', location)
            if m:
                self.service = m.group(1)

        if not row:
            self.name = node_name
            self.type = 'dead'
            return

        self.name = row['node_name']
        self.type = row['node_type']
        self.global_watermark = row['global_watermark']
        self.local_watermark = row['local_watermark']
        self.completed_tick = row['worker_last_tick']
        self.provider_node = row['provider_node']
        self.provider_location = row['provider_location']
        self.consumer_name = row['worker_name']
        self.worker_name = row['worker_name']
        self.paused = row['worker_paused']
        self.uptodate = row['worker_uptodate']
        self.combined_queue = row['combined_queue']
        self.combined_type = row['combined_type']
        self.last_tick = row['worker_last_tick']
        try:
            target_for = row['target_for']
            if isinstance(target_for, str):
                self.target_for = skytools.parse_pgarray(target_for)
            else:
                self.target_for = target_for
        except KeyError:
            pass

        self.node_attrs = {}
        if 'node_attrs' in row:
            a = row['node_attrs']
            if a:
                self.node_attrs = skytools.db_urldecode(a)

    def __get_target_queue(self):
        qname = None
        if self.type == LEAF:
            if self.combined_queue:
                qname = self.combined_queue
            else:
                return None
        else:
            qname = self.queue_name
        if qname is None:
            raise Exception("no target queue")
        return qname

    def get_title(self):
        if self.service:
            return "%s (%s, %s)" % (self.name, self.type, self.service)
        return "%s (%s)" % (self.name, self.type)

    def get_infolines(self):
        lst = self._info_lines

        lag = None
        if self.parent:
            root = self.parent
            while root.parent:
                root = root.parent
            cinfo = self.parent.consumer_map.get(self.consumer_name)
            if cinfo and root.queue_info:
                tick_time = cinfo['tick_time']
                root_time = root.queue_info['now']
                if root_time < tick_time:
                    # ignore negative lag - probably due to info gathering
                    # taking long time
                    lag = datetime.timedelta(0)
                else:
                    lag = root_time - tick_time
        elif self.queue_info:
            lag = self.queue_info['ticker_lag']

        c1 = ""
        c2 = ""
        use_colors = sys.stdout.isatty()
        if use_colors:
            if sys.platform.startswith('win'):
                use_colors = False

        if use_colors:
            if lag and lag > datetime.timedelta(minutes=1):
                c1 = "\033[31m"  # red
                c2 = "\033[0m"

        txt = "Lag: %s%s%s" % (c1, lag and ival2str(lag) or "(n/a)", c2)
        if self.last_tick:
            txt += ", Tick: %s" % self.last_tick
        if self.paused:
            txt += ", PAUSED"
        if not self.uptodate:
            txt += ", NOT UPTODATE"
        lst.append(txt)

        for k, v in self.node_attrs.items():
            txt = "Attr: %s=%s" % (k, v)
            lst.append(txt)

        for cname, row in self.cascaded_consumer_map.items():
            err = row['cur_error']
            if err:
                # show only first line
                pos = err.find('\n')
                if pos > 0:
                    err = err[:pos]
                lst.append("ERR: %s: %s" % (cname, err))
        return lst

    def add_info_line(self, ln):
        self._info_lines.append(ln)

    def load_status(self, curs):
        self.consumer_map = {}
        self.queue_info = {}
        self.cascaded_consumer_map = {}
        if self.queue_name:
            q = "select consumer_name, current_timestamp - lag as tick_time,"\
                "  lag, last_seen, last_tick "\
                "from pgq.get_consumer_info(%s)"
            curs.execute(q, [self.queue_name])
            for row in curs.fetchall():
                cname = row['consumer_name']
                self.consumer_map[cname] = row

            q = "select current_timestamp - ticker_lag as tick_time,"\
                "  ticker_lag, current_timestamp as now "\
                "from pgq.get_queue_info(%s)"
            curs.execute(q, [self.queue_name])
            self.queue_info = curs.fetchone()

            q = "select * from pgq_node.get_consumer_info(%s)"
            curs.execute(q, [self.queue_name])
            for row in curs.fetchall():
                cname = row['consumer_name']
                self.cascaded_consumer_map[cname] = row


class QueueInfo(object):
    """Info about cascaded queue.

    Slightly broken, as all info is per-node.
    """

    def __init__(self, queue_name, info_row, member_rows):
        self.member_map = {}
        for r in member_rows:
            m = MemberInfo(r)
            self._add_member(m)

        m = self.member_map.get(info_row['node_name'])

        self.local_node = NodeInfo(queue_name, info_row, location=m and m.location)
        self.queue_name = queue_name
        self.node_map = {}
        self.add_node(self.local_node)

    def _add_member(self, member):
        self.member_map[member.name] = member

    def get_member(self, name):
        return self.member_map.get(name)

    def get_node(self, name):
        return self.node_map.get(name)

    def add_node(self, node):
        self.node_map[node.name] = node

    def tag_dead(self, node_name):
        if node_name in self.node_map:
            self.member_map[node_name].dead = True
        else:
            row = {'node_name': node_name, 'node_location': None, 'dead': True}
            m = MemberInfo(row)
            self.member_map[node_name] = m
    #
    # Rest is about printing the tree
    #

    _DATAFMT = "%-30s%s"

    def print_tree(self):
        """Print ascii-tree for set.
        Expects that data for all nodes is filled in."""

        print('Queue: %s   Local node: %s' % (self.queue_name, self.local_node.name))
        print('')

        root_list = self._prepare_tree()
        for root in root_list:
            self._tree_calc(root)
            datalines = self._print_node(root, '', [])
            for ln in datalines:
                print(self._DATAFMT % (' ', ln))

    def print_tree_compact(self):
        """Print ascii-tree for set in compact format.
        Expects that data for all nodes is filled in."""

        print('Queue: %s   Local node: %s' % (self.queue_name, self.local_node.name))
        print('')

        root_list = self._prepare_tree()
        for root in root_list:
            self._tree_calc(root)
            self._print_node_compact(root, '')

    def _print_node_compact(self, node, pfx):
        print(pfx + node.get_title().ljust(60-len(pfx)) + ''.join(ln.ljust(30) for ln in node.get_infolines()))
        for n in node.child_list:
            self._print_node_compact(n, pfx + '   ')

    def _print_node(self, node, pfx, datalines):
        # print a tree fragment for node and info
        # returns list of unprinted data rows
        for ln in datalines:
            print(self._DATAFMT % (_setpfx(pfx, '|'), ln))
        datalines = node.get_infolines()
        print("%s%s" % (_setpfx(pfx, '+--: '), node.get_title()))

        for i, n in enumerate(node.child_list):
            sfx = ((i < len(node.child_list) - 1) and '  |' or '   ')
            datalines = self._print_node(n, pfx + sfx, datalines)

        return datalines

    def _prepare_tree(self):
        # reset vars, fill parent and child_list for each node
        # returns list of root nodes (mostly 1)

        for node in self.node_map.values():
            node.total_childs = 0
            node.levels = 0
            node.child_list = []
            node.parent = None

        root_list = []
        for node in self.node_map.values():
            if node.type != 'root' \
                    and node.provider_node \
                    and node.provider_node != node.name \
                    and node.provider_node in self.node_map:
                p = self.node_map[node.provider_node]
                p.child_list.append(node)
                node.parent = p
            else:
                node.parent = None
                root_list.append(node)
        return root_list

    def _tree_calc(self, node):
        # calculate levels and count total childs
        # sort the tree based on them
        total = len(node.child_list)
        levels = 1
        for subnode in node.child_list:
            self._tree_calc(subnode)
            total += subnode.total_childs
            if levels < subnode.levels + 1:
                levels = subnode.levels + 1
        node.total_childs = total
        node.levels = levels
        node.child_list.sort(key=_node_key)


def _setpfx(pfx, sfx):
    if pfx:
        pfx = pfx[:-1] + sfx
    return pfx


def _node_key(n):
    return (n.levels, n.total_childs, n.service or '', n.name)

