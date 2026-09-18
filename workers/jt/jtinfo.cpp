// jtinfo - diagnose JT files: version, segment inventory, LSG structure census.
//
// Same GPL-2.0 family as jt2pkg (links TKJT): runs behind the API or on a workstation,
// never shipped in a product. Used to survey a JT collection before converting it:
// which versions, monolithic vs shattered, tessellated vs XT-BRep-only.

#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include <JtData_Model.hxx>
#include <JtNode_Base.hxx>
#include <JtNode_Partition.hxx>
#include <JtNode_Part.hxx>
#include <JtNode_MetaData.hxx>
#include <JtNode_RangeLOD.hxx>
#include <JtNode_LOD.hxx>
#include <JtNode_Group.hxx>
#include <JtNode_Instance.hxx>
#include <JtNode_Shape_TriStripSet.hxx>
#include <JtProperty_LateLoaded.hxx>

static std::string segName(int t) {
  if (t == 1) return "LSG";
  if (t == 2) return "XT-BRep";
  if (t == 3) return "PMI";
  if (t == 4) return "Meta";
  if (t == 6) return "Shape";
  if (t >= 7 && t <= 16) return "LOD" + std::to_string(t - 7);
  return "type" + std::to_string(t);
}

struct Census {
  std::map<std::string, long long> nodes;
  long long shapeNodes = 0, lodRefs = 0;
  int depth = 0, maxDepth = 0;
};

static void census(const Handle(JtNode_Base)& node, Census& c) {
  if (node.IsNull() || ++c.depth > 500) return;
  c.maxDepth = std::max(c.maxDepth, c.depth);
  std::string kind = "other";
  Handle(JtNode_Group) group;
  if (!Handle(JtNode_Partition)::DownCast(node).IsNull()) kind = "Partition";
  else if (!Handle(JtNode_Part)::DownCast(node).IsNull()) kind = "Part";
  else if (!Handle(JtNode_RangeLOD)::DownCast(node).IsNull()) kind = "RangeLOD";
  else if (!Handle(JtNode_LOD)::DownCast(node).IsNull()) kind = "LOD";
  else if (!Handle(JtNode_MetaData)::DownCast(node).IsNull()) kind = "MetaData";
  else if (!Handle(JtNode_Instance)::DownCast(node).IsNull()) {
    kind = "Instance";
    Handle(JtNode_Base) t = Handle(JtNode_Base)::DownCast(
      Handle(JtNode_Instance)::DownCast(node)->Object());
    if (!t.IsNull()) census(t, c);
    c.depth--;
    c.nodes[kind]++;
    return;
  } else if (!Handle(JtNode_Shape_TriStripSet)::DownCast(node).IsNull()) {
    kind = "Shape";
    c.shapeNodes++;
    c.lodRefs += (long long)Handle(JtNode_Shape_TriStripSet)::DownCast(node)->LateLoads().Count();
    c.depth--;
    c.nodes[kind]++;
    return;
  } else if (!Handle(JtNode_Group)::DownCast(node).IsNull()) kind = "Group";
  c.nodes[kind]++;
  group = Handle(JtNode_Group)::DownCast(node);
  if (!group.IsNull()) {
    const auto& kids = group->Children();
    for (size_t i = 0; i < kids.Count(); ++i) {
      Handle(JtNode_Base) k = Handle(JtNode_Base)::DownCast(kids[i]);
      // NOTE: a Partition child with a file name is an external reference; Partition::Load()
      // must NOT be called (it corrupts handle refcounts - see jt2pkg.cpp). Count, don't load.
      if (!k.IsNull()) census(k, c);
    }
  }
  c.depth--;
}

int main(int argc, char** argv) {
  if (argc < 2) { fprintf(stderr, "usage: jtinfo file.jt [more.jt ...]\n"); return 1; }
  for (int a = 1; a < argc; ++a) {
    TCollection_ExtendedString fn(argv[a]);
    Handle(JtData_Model) model = new JtData_Model(fn);
    Handle(JtNode_Partition) root;
    try {
      root = model->Init();
    } catch (...) {
      printf("%s: PARSE FAILED\n", argv[a]);
      continue;
    }
    printf("%s: JT %d.%d %s", argv[a], model->MajorVersion(), model->MinorVersion(),
           model->IsFileLE() ? "LE" : "BE");
    std::map<int, long long> segs;
    long long totalLen = 0;
    for (const auto& e : model->getTOC()) { segs[e.Type]++; totalLen += e.Length; }
    printf(", %d segments (%lld bytes): ", (int)model->getTOC().size(), totalLen);
    bool first = true;
    for (const auto& kv : segs) {
      printf("%s%s=%lld", first ? "" : " ", segName(kv.first).c_str(), kv.second);
      first = false;
    }
    if (root.IsNull()) {
      printf(" | NO LSG SEGMENT\n");
      continue;
    }
    Census c;
    census(root, c);
    printf(" | LSG depth %d:", c.maxDepth);
    for (const auto& kv : c.nodes) printf(" %s=%lld", kv.first.c_str(), kv.second);
    printf(" | shape LOD refs: %lld", c.lodRefs);
    // External references named by partitions (not loaded, see note above).
    printf("\n");
  }
  return 0;
}
