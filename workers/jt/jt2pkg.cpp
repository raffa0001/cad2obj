// jt2pkg - JT -> OBJ model package.
//
// Reads a JT file (v8/9/10 tessellated data) with Open CASCADE's TKJT reader and writes the
// package described in docs/PACKAGE.md: a mesh in METRES with one material per part, a
// `.settings` sidecar the tracker reads beside the mesh, and a report.
//
// What JT gives us and what it does not:
//   * LODs, not surfaces. A JT file carries authored tessellations (triangle strips decoded
//     to plain triangles by TKJT), never the exact B-rep the STEP worker re-tessellates.
//     The budget loop therefore steps DOWN through authored LODs instead of re-meshing.
//   * NO UNITS. The JT format stores no length unit and TKJT exposes none. Real files are
//     almost always millimetres (Siemens NX convention) but that is a guess, so the worker
//     takes --assume-units (default mm) and the report says ASSUMED, never declared.
//   * Assembly structure and names, like STEP. Each placed shape occurrence is a part.
//
// LICENSING - READ THIS. TKJT is GPL-2.0, so THIS BINARY is GPL-2.0. It runs only as a
// subprocess behind the API (see cad2obj/api.py) and is NEVER linked into or shipped
// with anything we distribute (tracker, app, editor, client tools). Its sources live in
// vendor/ (git-ignored) and are compiled straight into this executable.

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <string>
#include <unordered_map>
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
#include <JtElement_ShapeLOD_Vertex.hxx>
#include <JtProperty_LateLoaded.hxx>
#include <JtAttribute_Base.hxx>
#include <JtAttribute_Material.hxx>
#include <JtAttribute_GeometricTransform.hxx>

namespace fs = std::filesystem;

static const int PACKAGE_VERSION = 1;
// Coordinates closer than this (metres) are the same vertex. JT stores one vertex pool per
// shape so welding mostly removes strip-seam duplicates; the tracker welds on load anyway,
// but a package that arrives welded has honest vertex counts in its report.
static const double WELD_M = 1e-7;

// --- small helpers ----------------------------------------------------------------------------

static std::string jsonEscape(const std::string& s) {
  std::string o;
  for (unsigned char c : s) {
    switch (c) {
      case '"': o += "\\\""; break;
      case '\\': o += "\\\\"; break;
      case '\n': o += "\\n"; break;
      case '\r': o += "\\r"; break;
      case '\t': o += "\\t"; break;
      default:
        if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
  }
  return o;
}

// Shortest round-trip-ish decimal: %.6f with trailing zeros stripped (matches the STEP
// worker's round(x, 6) closely enough that reports stay comparable).
static std::string num6(double v) {
  if (!std::isfinite(v)) return "0";
  char b[40]; snprintf(b, sizeof b, "%.6f", v);
  std::string s = b;
  if (s.find('.') != std::string::npos) {
    while (!s.empty() && s.back() == '0') s.pop_back();
    if (!s.empty() && s.back() == '.') s.pop_back();
  }
  return s.empty() ? "0" : s;
}

static std::string num4(double v) {
  char b[32]; snprintf(b, sizeof b, "%.4f", (double)(float)v);
  return b;
}

// TCollection_ExtendedString (UTF-16-ish storage) -> UTF-8, with surrogate-pair combining
// so astral-plane characters survive whatever the underlying char width is.
static std::string toUtf8(const TCollection_ExtendedString& s) {
  std::string o;
  Standard_Integer n = s.Length();
  for (Standard_Integer i = 1; i <= n; ++i) {
    uint32_t u = (uint32_t)(uint16_t)s.Value(i);
    if (u >= 0xD800 && u <= 0xDBFF && i < n) {
      uint32_t lo = (uint32_t)(uint16_t)s.Value(i + 1);
      if (lo >= 0xDC00 && lo <= 0xDFFF) { u = 0x10000 + ((u - 0xD800) << 10) + (lo - 0xDC00); ++i; }
    }
    if (u < 0x80) o += (char)u;
    else if (u < 0x800) { o += (char)(0xC0 | (u >> 6)); o += (char)(0x80 | (u & 0x3F)); }
    else if (u < 0x10000) { o += (char)(0xE0 | (u >> 12)); o += (char)(0x80 | ((u >> 6) & 0x3F)); o += (char)(0x80 | (u & 0x3F)); }
    else { o += (char)(0xF0 | (u >> 18)); o += (char)(0x80 | ((u >> 12) & 0x3F)); o += (char)(0x80 | ((u >> 6) & 0x3F)); o += (char)(0x80 | (u & 0x3F)); }
  }
  return o;
}

// Like the STEP worker's _obj_safe: keep what OBJ and the tracker tolerate.
static std::string objSafe(const std::string& s) {
  std::string o;
  for (unsigned char c : s) {
    if (std::isalnum(c) || c == '.' || c == '_' || c == '-' || c == '/') o += (char)c;
    else o += '_';
  }
  return o.empty() ? "part" : o;
}

// The JTAssistant viewer strips CAD export suffixes from names; keep that so part names
// match what other tools from this reader show.
static std::string cleanName(const std::string& s) {
  std::string o = s;
  for (const char* suf : {".asm", ".part"}) {
    size_t n = strlen(suf);
    if (o.size() > n && o.compare(o.size() - n, n, suf) == 0) { o.resize(o.size() - n); break; }
  }
  return o;
}

// --- 4x4 matrix, column-major like the JT transform array --------------------------------------
// (m[col*4+row]). Kept manual on purpose: JT transforms may carry mirrors/non-uniform
// scale that gp_Trsf cannot represent, and the two reference viewers both read the raw
// array this way.

struct Mat4 {
  double m[16];
  static Mat4 identity() {
    Mat4 t; for (int i = 0; i < 16; ++i) t.m[i] = (i % 5 == 0) ? 1.0 : 0.0; return t;
  }
  // this * b: apply b first, then this (parent * local down the tree).
  Mat4 operator*(const Mat4& b) const {
    Mat4 c;
    for (int col = 0; col < 4; ++col)
      for (int row = 0; row < 4; ++row) {
        double s = 0;
        for (int k = 0; k < 4; ++k) s += m[k * 4 + row] * b.m[col * 4 + k];
        c.m[col * 4 + row] = s;
      }
    return c;
  }
  void apply(double& x, double& y, double& z) const {
    double nx = m[0] * x + m[4] * y + m[8] * z + m[12];
    double ny = m[1] * x + m[5] * y + m[9] * z + m[13];
    double nz = m[2] * x + m[6] * y + m[10] * z + m[14];
    x = nx; y = ny; z = nz;
  }
};

// --- conversion state -------------------------------------------------------------------------

struct Lod {
  std::vector<float> v;    // xyz, FILE units
  std::vector<int32_t> t;  // triples into v/3
  int segType = 0;         // JT segment type: 6 = full shape, 7..16 = authored LOD0..LOD9
};

struct RawPart {
  std::string name, path;
  Mat4 trsf;
  float color[3] = {0.7f, 0.7f, 0.7f};
  std::vector<Lod> lods;   // file order; choice is an index into the desc-by-size order
  std::vector<int> order;  // lods sorted finest-first
  int choice = 0;          // rank into order[]
  std::vector<std::string> warnings;
};

struct Frame {
  std::string path;
  Mat4 trsf = Mat4::identity();
  float color[3] = {0.7f, 0.7f, 0.7f};
  bool hasColor = false;
  int depth = 0;
  Handle(JtData_Model) model;  // file this subtree comes from (segment lookups, children)
  std::string dir;             // that file's folder: external refs resolve against it
  std::string file;            // that file's canonical path: self-refs and cycles resolve against it
};

struct Ctx {
  int maxTriangles = 20000;
  std::vector<RawPart> parts;
  std::vector<std::string> externalFiles;
  std::vector<Handle(JtData_Model)> models;  // every opened file, kept alive to the end
  std::vector<std::string> openStack;        // ancestor files of the current node: true cycles
  bool sawRangeLod = false, sawLod = false, sawUnsupportedShape = false;
  bool sawCaseFallback = false;              // a Windows-cased reference matched case-insensitively
  size_t maxParts = 200000;
  std::string error;
};

static void sortLods(RawPart& rp) {
  rp.order.resize(rp.lods.size());
  for (size_t i = 0; i < rp.order.size(); ++i) rp.order[i] = (int)i;
  // Finest first: authored LOD level first (Shape=6, LOD0=7, ...), triangle count breaks
  // ties and covers files that do not tag their segments.
  std::sort(rp.order.begin(), rp.order.end(), [&](int a, int b) {
    int sa = rp.lods[a].segType ? rp.lods[a].segType : 1000;
    int sb = rp.lods[b].segType ? rp.lods[b].segType : 1000;
    if (sa != sb) return sa < sb;
    return rp.lods[a].t.size() > rp.lods[b].t.size();
  });
  rp.choice = 0;
}

// LOD choice: finest everywhere, then step every part down one authored level at a time
// until the whole model fits the tracker's budget (mirrors the STEP worker's passes,
// but follows authored LODs instead of re-tessellating - JT has no exact surfaces).
// Returns the number of step-down rounds.
static int selectLods(Ctx& ctx) {
  auto totalTris = [&]() {
    long long n = 0;
    for (auto& p : ctx.parts)
      if (!p.lods.empty()) n += (long long)p.lods[p.order[p.choice]].t.size() / 3;
    return n;
  };
  int stepDowns = 0;
  while (ctx.maxTriangles > 0 && totalTris() > ctx.maxTriangles) {
    bool moved = false;
    for (auto& p : ctx.parts)
      if (!p.lods.empty() && (size_t)(p.choice + 1) < p.order.size()) { p.choice++; moved = true; }
    if (!moved) break;
    stepDowns++;
  }
  return stepDowns;
}

static void applyAttributes(const Handle(JtNode_Base)& node, Frame& f) {
  const JtData_Object::VectorOfObjects& attrs = node->Attributes();
  for (size_t i = 0; i < attrs.Count(); ++i) {
    Handle(JtData_Object) o = attrs[i];
    if (o.IsNull()) continue;
    if (Handle(JtAttribute_GeometricTransform) t = Handle(JtAttribute_GeometricTransform)::DownCast(o)) {
      const Standard_Real* a = t->GetTrsf();
      Mat4 local; for (int k = 0; k < 16; ++k) local.m[k] = a[k];
      f.trsf = f.trsf * local;
    } else if (Handle(JtAttribute_Material) mtl = Handle(JtAttribute_Material)::DownCast(o)) {
      const Jt_F32* d = mtl->DiffuseColor();
      f.color[0] = d[0]; f.color[1] = d[1]; f.color[2] = d[2];
      f.hasColor = true;
    }
  }
}

static void walk(const Handle(JtNode_Base)& node, Frame f, Ctx& ctx);

static void walkChildren(const Handle(JtNode_Group)& g, const Frame& f, Ctx& ctx) {
  const JtData_Object::VectorOfObjects& kids = g->Children();
  for (size_t i = 0; i < kids.Count(); ++i) {
    Handle(JtNode_Base) c = Handle(JtNode_Base)::DownCast(kids[i]);
    if (!c.IsNull()) walk(c, f, ctx);
    if (!ctx.error.empty()) return;
  }
}

static void walk(const Handle(JtNode_Base)& node, Frame f, Ctx& ctx) {
  if (!ctx.error.empty() || ctx.parts.size() >= ctx.maxParts) return;
  if (++f.depth > 500) { ctx.error = "assembly nesting deeper than 500 - refusing (cyclic instances?)"; return; }

  std::string name = cleanName(toUtf8(node->Name()));

  if (Handle(JtNode_Partition) p = Handle(JtNode_Partition)::DownCast(node)) {
    // A partition with a file name references an EXTERNAL .jt file (a shattered assembly).
    // NOTE: never call p->Load() here. It assigns the child's vector with a raw memcpy that
    // bypasses OCCT handle refcounts, so the loaded subtree is freed while still referenced
    // (use-after-free, seen as a segfault on CoffeeMaker.jt). Open the file ourselves and
    // keep its model handle alive in ctx.models instead.
    if (p->FileName().Length() > 0) {
      std::string ext = toUtf8(p->FileName());
      fs::path childPath(ext);
      if (!childPath.is_absolute()) childPath = fs::path(f.dir) / childPath;
      std::error_code ec;
      // SELF-IDENTIFICATION first, before touching the disk: JT roots routinely name their
      // own file, sometimes with a historical relative path ("./fishing_reel/drag_knob.jt")
      // that resolves nowhere. The scene is already inline below; loading it again would
      // recurse forever.
      bool isSelf = childPath.filename() == fs::path(f.file).filename();
      std::string canon;
      if (!isSelf) {
        // Shattered assemblies are usually authored on Windows: the reference may differ in
        // case from the file on disk ("Part1.jt" vs "part1.jt"). Try exact first, then a
        // case-insensitive scan of the reference's own folder.
        if (fs::is_regular_file(childPath, ec)) {
          canon = fs::weakly_canonical(childPath, ec).string();
        } else {
          // Look in the reference's own folder: it may carry a subdirectory ("./parts/Part1.jt").
        fs::path refDir = childPath.parent_path();
        std::string want = childPath.filename().string();
        std::string lower = want;
        std::transform(lower.begin(), lower.end(), lower.begin(), ::tolower);
        std::error_code ec2;
        for (const auto& e : fs::directory_iterator(refDir, ec2)) {
          std::string have = e.path().filename().string();
          std::string havel = have;
          std::transform(havel.begin(), havel.end(), havel.begin(), ::tolower);
          if (havel == lower) {
            childPath = e.path();
            ctx.sawCaseFallback = true;
            break;
          }
        }
        if (fs::is_regular_file(childPath, ec)) canon = fs::weakly_canonical(childPath, ec).string();
        }
        if (canon.empty() || ec) { ctx.error = "referenced JT file not found: " + ext; return; }
        isSelf = canon == f.file;
      }
      if (isSelf) {
        // inline scene below; nothing to load.
      } else if (std::find(ctx.openStack.begin(), ctx.openStack.end(), canon) != ctx.openStack.end()) {
        ctx.error = "cyclic JT file reference: " + ext; return;
      } else {
        TCollection_ExtendedString cfname(canon.c_str());
        Handle(JtData_Model) child = new JtData_Model(cfname, f.model);
        Handle(JtNode_Partition) childRoot;
        try {
          childRoot = child->Init();
        } catch (...) {
          ctx.error = "could not parse referenced JT file: " + ext; return;
        }
        if (childRoot.IsNull()) { ctx.error = "no scene graph in referenced JT file: " + ext; return; }
        ctx.models.push_back(child);
        ctx.externalFiles.push_back(ext);
        if (!name.empty()) f.path = f.path.empty() ? name : f.path + "/" + name;
        applyAttributes(node, f);
        Frame cf = f;
        cf.model = child;
        cf.dir = fs::path(canon).parent_path().string();
        cf.file = canon;
        ctx.openStack.push_back(canon);
        walkChildren(childRoot, cf, ctx);
        ctx.openStack.pop_back();
        return;
      }
    }
    if (!name.empty()) f.path = f.path.empty() ? name : f.path + "/" + name;
    applyAttributes(node, f);
    walkChildren(p, f, ctx);
  } else if (Handle(JtNode_Part) part = Handle(JtNode_Part)::DownCast(node)) {
    // A part is a real product occurrence - it extends the path its shapes report under.
    std::string n = name.empty() ? "part" : name;
    f.path = f.path.empty() ? n : f.path + "/" + n;
    applyAttributes(node, f);
    walkChildren(part, f, ctx);
  } else if (Handle(JtNode_RangeLOD) r = Handle(JtNode_RangeLOD)::DownCast(node)) {
    // Range-LOD children are the SAME geometry at different viewing distances, not more
    // geometry: exporting all of them would stack overlapping shells and double the
    // triangles. Keep the first (finest) child only.
    ctx.sawRangeLod = true;
    applyAttributes(node, f);
    const JtData_Object::VectorOfObjects& kids = r->Children();
    if (kids.Count() > 0) {
      Handle(JtNode_Base) c = Handle(JtNode_Base)::DownCast(kids[0]);
      if (!c.IsNull()) walk(c, f, ctx);
    }
  } else if (Handle(JtNode_LOD) l = Handle(JtNode_LOD)::DownCast(node)) {
    ctx.sawLod = true;
    applyAttributes(node, f);
    const JtData_Object::VectorOfObjects& kids = l->Children();
    if (kids.Count() > 0) {
      Handle(JtNode_Base) c = Handle(JtNode_Base)::DownCast(kids[0]);
      if (!c.IsNull()) walk(c, f, ctx);
    }
  } else if (Handle(JtNode_MetaData) md = Handle(JtNode_MetaData)::DownCast(node)) {
    if (!name.empty()) f.path = f.path.empty() ? name : f.path + "/" + name;
    applyAttributes(node, f);
    walkChildren(md, f, ctx);
  } else if (Handle(JtNode_Instance) inst = Handle(JtNode_Instance)::DownCast(node)) {
    // Decompose like the JTAssistant viewer: the SAME shape placed six times is six parts,
    // because each occurrence can be present or missing on the tracked object.
    if (!name.empty()) f.path = f.path.empty() ? name : f.path + "/" + name;
    applyAttributes(node, f);
    Handle(JtNode_Base) target = Handle(JtNode_Base)::DownCast(inst->Object());
    if (!target.IsNull()) walk(target, f, ctx);
  } else if (Handle(JtNode_Shape_TriStripSet) shape = Handle(JtNode_Shape_TriStripSet)::DownCast(node)) {
    applyAttributes(node, f);
    RawPart rp;
    rp.name = name.empty() ? "part" : name;
    rp.path = f.path.empty() ? rp.name : f.path + "/" + rp.name;
    rp.trsf = f.trsf;
    if (f.hasColor) { rp.color[0] = f.color[0]; rp.color[1] = f.color[1]; rp.color[2] = f.color[2]; }
    const JtData_Object::VectorOfLateLoads& ll = shape->LateLoads();
    for (size_t i = 0; i < ll.Count(); ++i) {
      Handle(JtProperty_LateLoaded) prop = ll[i];
      if (prop.IsNull()) continue;
      if (prop->DefferedObject().IsNull()) prop->Load();
      int segType = prop->getSegmentType();
      Handle(JtElement_ShapeLOD_Vertex) lod =
        Handle(JtElement_ShapeLOD_Vertex)::DownCast(prop->DefferedObject());
      prop->Unload();  // drops the reader's reference; ours below keeps the data alive
      if (lod.IsNull()) { ctx.sawUnsupportedShape = true; continue; }
      const auto& idx = lod->Indices();
      const auto& vtx = lod->Vertices();
      if (idx.Count() < 3 || vtx.Count() == 0) continue;
      Lod l;
      l.segType = segType;
      l.v.resize((size_t)vtx.Count() * 3);
      memcpy(l.v.data(), vtx.Data(), l.v.size() * sizeof(float));
      size_t ntri = (size_t)idx.Count() / 3;
      l.t.resize(ntri * 3);
      memcpy(l.t.data(), idx.Data(), l.t.size() * sizeof(int32_t));
      rp.lods.push_back(std::move(l));
    }
    if (rp.lods.empty()) {
      rp.warnings.push_back("no tessellated LODs - left out of the mesh");
      ctx.parts.push_back(std::move(rp));
      return;
    }
    sortLods(rp);
    ctx.parts.push_back(std::move(rp));
  } else if (Handle(JtNode_Group) g = Handle(JtNode_Group)::DownCast(node)) {
    if (!name.empty()) f.path = f.path.empty() ? name : f.path + "/" + name;
    applyAttributes(node, f);
    walkChildren(g, f, ctx);
  } else {
    ctx.sawUnsupportedShape = true;  // e.g. a point-set or wireframe shape node: nothing to track
  }
}

// --- entry point ------------------------------------------------------------------------------

static void fail(const std::string& msg) {
  // The API reads the LAST stderr line as JSON {"error": ...}; make sure it is ours.
  fprintf(stderr, "{\"error\": \"%s\"}\n", jsonEscape(msg).c_str());
  exit(1);
}

int main(int argc, char** argv) {
  std::string input, outDir, assumeUnits = "mm";
  int maxTriangles = 20000;
  bool selfTest = false;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    if (a == "--max-triangles" && i + 1 < argc) maxTriangles = atoi(argv[++i]);
    else if (a == "--assume-units" && i + 1 < argc) assumeUnits = argv[++i];
    else if (a == "--self-test") selfTest = true;
    else if (a.rfind("--", 0) == 0) fail("unknown option: " + a);
    else if (input.empty()) input = a;
    else if (outDir.empty()) outDir = a;
    else fail("too many positional arguments");
  }
  if (selfTest) {
    // Exercises the REAL sortLods()/selectLods() on synthetic parts (no JT reader
    // involved): the sample files on hand all carry one LOD per shape, so this is how
    // the budget step-down is covered. Prints one JSON line for pytest to assert on.
    //   A: segTags out of order {9,7,8} x {100,1000,500} tris -> finest-first [1000,500,100]
    //   B: untagged {300,100} -> by count [300,100].  C: single 50.
    auto mkpart = [](std::vector<std::pair<int, int>> segTris) {
      RawPart p;
      for (auto st : segTris) {
        Lod l;
        l.segType = st.first;
        l.t.assign((size_t)st.second * 3, 0);
        p.lods.push_back(std::move(l));
      }
      sortLods(p);
      return p;
    };
    auto runCase = [&](int budget) {
      Ctx c;
      c.maxTriangles = budget;
      c.parts.push_back(mkpart({{9, 100}, {7, 1000}, {8, 500}}));
      c.parts.push_back(mkpart({{0, 300}, {0, 100}}));
      c.parts.push_back(mkpart({{7, 50}}));
      int steps = selectLods(c);
      long long total = 0;
      std::string choices;
      for (auto& p : c.parts) {
        total += (long long)p.lods[p.order[p.choice]].t.size() / 3;
        if (!choices.empty()) choices += ",";
        choices += std::to_string(p.choice);
      }
      char b[160];
      snprintf(b, sizeof b, "{\"budget\":%d,\"steps\":%d,\"choices\":[%s],\"total\":%lld}",
               budget, steps, choices.c_str(), total);
      return std::string(b);
    };
    // Finest-first order check on part A (segTag, tris): expect [7/1000, 8/500, 9/100].
    Ctx c0;
    c0.parts.push_back(mkpart({{9, 100}, {7, 1000}, {8, 500}}));
    std::string order;
    for (int ix : c0.parts[0].order) {
      if (!order.empty()) order += ",";
      order += std::to_string(c0.parts[0].lods[ix].segType);
    }
    printf("{\"order\":[%s],\"cases\":[%s,%s,%s]}\n", order.c_str(),
           runCase(1200).c_str(), runCase(0).c_str(), runCase(10).c_str());
    return 0;
  }
  if (input.empty() || outDir.empty()) fail("usage: jt2pkg input.jt out_dir [--max-triangles N] [--assume-units mm|cm|m|in|ft] [--self-test]");

  double toM = 0;
  std::string unitName;
  if (assumeUnits == "mm") { toM = 0.001; unitName = "millimetre"; }
  else if (assumeUnits == "cm") { toM = 0.01; unitName = "centimetre"; }
  else if (assumeUnits == "m") { toM = 1.0; unitName = "metre"; }
  else if (assumeUnits == "in") { toM = 0.0254; unitName = "inch"; }
  else if (assumeUnits == "ft") { toM = 0.3048; unitName = "foot"; }
  else fail("--assume-units must be one of mm|cm|m|in|ft");

  double t0 = (double)clock() / CLOCKS_PER_SEC;

  fs::path inAbs = fs::absolute(input), outAbs = fs::absolute(outDir);
  std::error_code ec0;
  std::string inCanon = fs::weakly_canonical(inAbs, ec0).string();
  if (ec0) fail("cannot resolve input path");

  TCollection_ExtendedString fname(inCanon.c_str());
  Handle(JtData_Model) model = new JtData_Model(fname);
  Handle(JtNode_Partition) root;
  try {
    root = model->Init();
  } catch (...) {
    fail("JT parse failed (exception during header/TOC read)");
  }
  if (root.IsNull()) fail("no logical scene graph in this file (partition node missing)");
  std::string jtVersion = std::to_string(model->MajorVersion()) + "." + std::to_string(model->MinorVersion());

  Ctx ctx;
  ctx.maxTriangles = maxTriangles;
  ctx.models.push_back(model);   // the root file stays alive like every external one
  ctx.openStack.push_back(inCanon);
  Frame top;
  top.model = model;
  top.dir = fs::path(inCanon).parent_path().string();
  top.file = inCanon;
  try {
    walk(root, top, ctx);
  } catch (...) {
    fail("JT traversal failed (exception while reading geometry segments)");
  }
  if (!ctx.error.empty()) fail(ctx.error);
  if (ctx.parts.size() >= ctx.maxParts) fail("more than 200000 shape occurrences - refusing");
  double tRead = (double)clock() / CLOCKS_PER_SEC - t0;

  // LOD choice via selectLods() (same function the --self-test below exercises).
  int stepDowns = selectLods(ctx);

  // Bake: transform to metres, weld, drop degenerates, measure from the mesh.
  struct Baked { std::vector<float> v; std::vector<int32_t> t; double box[6]; };
  std::vector<Baked> baked(ctx.parts.size());
  struct Key { int64_t x, y, z; bool operator==(const Key& o) const { return x == o.x && y == o.y && z == o.z; } };
  struct KeyH { size_t operator()(const Key& k) const noexcept {
    return (size_t)(k.x * 73856093LL ^ k.y * 19349663LL ^ k.z * 83492791LL); } };
  for (size_t pi = 0; pi < ctx.parts.size(); ++pi) {
    RawPart& p = ctx.parts[pi];
    Baked& b = baked[pi];
    if (p.lods.empty()) continue;
    const Lod& lod = p.lods[p.order[p.choice]];
    size_t nv = lod.v.size() / 3;
    std::unordered_map<Key, int32_t, KeyH> seen;
    seen.reserve(nv * 2);
    std::vector<int32_t> remap(nv, -1);
    for (size_t i = 0; i < nv; ++i) {
      // Transform FIRST in file units, scale to metres AFTER: the transform's translation
      // is in file units too (scaling vertices first and then translating added e.g. 8956
      // *metres* to a fishing-reel knob - a 99-metre reel).
      double x = lod.v[3 * i], y = lod.v[3 * i + 1], z = lod.v[3 * i + 2];
      p.trsf.apply(x, y, z);
      x *= toM; y *= toM; z *= toM;
      Key k{(int64_t)llround(x / WELD_M), (int64_t)llround(y / WELD_M), (int64_t)llround(z / WELD_M)};
      auto it = seen.find(k);
      if (it == seen.end()) {
        int32_t id = (int32_t)(b.v.size() / 3);
        seen[k] = id;
        b.v.push_back((float)x); b.v.push_back((float)y); b.v.push_back((float)z);
        remap[i] = id;
      } else remap[i] = it->second;
    }
    size_t ntri = lod.t.size() / 3;
    for (size_t i = 0; i < ntri; ++i) {
      int32_t a = lod.t[3 * i], bb = lod.t[3 * i + 1], c = lod.t[3 * i + 2];
      if (a < 0 || bb < 0 || c < 0 || (size_t)a >= nv || (size_t)bb >= nv || (size_t)c >= nv) continue;
      int32_t ra = remap[a], rb = remap[bb], rc = remap[c];
      if (ra != rb && rb != rc && ra != rc) { b.t.push_back(ra); b.t.push_back(rb); b.t.push_back(rc); }
    }
    if (!b.v.empty()) {
      double lo[3] = {1e300, 1e300, 1e300}, hi[3] = {-1e300, -1e300, -1e300};
      for (size_t i = 0; i < b.v.size() / 3; ++i)
        for (int k = 0; k < 3; ++k) {
          lo[k] = std::min(lo[k], (double)b.v[3 * i + k]);
          hi[k] = std::max(hi[k], (double)b.v[3 * i + k]);
        }
      for (int k = 0; k < 3; ++k) { b.box[k] = lo[k]; b.box[k + 3] = hi[k]; }
    }
    if (b.t.empty()) p.warnings.push_back("no triangles - left out of the mesh");
  }

  // Unique paths: the same bolt placed six times is six parts (#1..#6).
  std::unordered_map<std::string, int> seen, cnt;
  for (auto& p : ctx.parts) seen[p.path]++;
  for (auto& p : ctx.parts)
    if (seen[p.path] > 1) p.path += "#" + std::to_string(++cnt[p.path]);

  // Hidden parts, first cut (same heuristic as the STEP worker): a part whose box sits
  // entirely inside another part's is probably internal. Reported, never removed.
  for (size_t i = 0; i < ctx.parts.size(); ++i) {
    if (baked[i].v.empty()) continue;
    for (size_t j = 0; j < ctx.parts.size(); ++j) {
      if (i == j || baked[j].v.empty() || baked[j].t.empty()) continue;
      bool inside = true;
      for (int k = 0; k < 3 && inside; ++k)
        inside = baked[j].box[k] <= baked[i].box[k] && baked[i].box[k + 3] <= baked[j].box[k + 3];
      if (inside) {
        ctx.parts[i].warnings.push_back("bounding box inside '" + ctx.parts[j].path + "' - possibly hidden");
        break;
      }
    }
  }

  // --- write the package (same layout the STEP worker writes) -------------------------------
  std::error_code ec;
  fs::create_directories(outAbs, ec);
  if (ec) fail("cannot create output dir: " + ec.message());
  std::string base = inAbs.stem().string();
  std::string objName = base + ".obj", mtlName = base + ".mtl", setName = base + ".settings";

  FILE* mtl = fopen((outAbs / mtlName).c_str(), "w");
  if (!mtl) fail("cannot write MTL");
  fprintf(mtl, "# written by cad2obj jt2pkg, package v%d\n", PACKAGE_VERSION);
  for (size_t i = 0; i < ctx.parts.size(); ++i) {
    RawPart& p = ctx.parts[i];
    fprintf(mtl, "\nnewmtl part_%04d\n# %s\nKd %s %s %s\n", (int)i,
            p.path.c_str(), num4(p.color[0]).c_str(), num4(p.color[1]).c_str(), num4(p.color[2]).c_str());
  }
  fclose(mtl);

  FILE* obj = fopen((outAbs / objName).c_str(), "w");
  if (!obj) fail("cannot write OBJ");
  fprintf(obj, "# written by cad2obj jt2pkg, package v%d\n# units: metres\nmtllib %s\n",
          PACKAGE_VERSION, mtlName.c_str());
  long long offset = 1;
  for (size_t i = 0; i < ctx.parts.size(); ++i) {
    Baked& b = baked[i];
    if (b.t.empty()) continue;
    fprintf(obj, "\no %s\nusemtl part_%04d\n", objSafe(ctx.parts[i].path).c_str(), (int)i);
    for (size_t k = 0; k < b.v.size() / 3; ++k)
      fprintf(obj, "v %.7f %.7f %.7f\n", b.v[3 * k], b.v[3 * k + 1], b.v[3 * k + 2]);
    for (size_t k = 0; k < b.t.size() / 3; ++k)
      fprintf(obj, "f %lld %lld %lld\n", (long long)b.t[3 * k] + offset,
              (long long)b.t[3 * k + 1] + offset, (long long)b.t[3 * k + 2] + offset);
    offset += (long long)(b.v.size() / 3);
  }
  fclose(obj);

  FILE* set = fopen((outAbs / setName).c_str(), "w");
  if (!set) fail("cannot write settings");
  fprintf(set, "# written by cad2obj jt2pkg\n"
               "# The mesh is in metres, converted from ASSUMED %s units (JT stores no unit),\n"
               "# so the tracker's world maths can trust it from the first frame instead of measuring it.\n"
               "world_scale_known_1000x = 1000\n", unitName.c_str());
  fclose(set);

  // --- report ---------------------------------------------------------------------------------
  long long totalT = 0, totalV = 0;
  double allBox[6] = {1e300, 1e300, 1e300, -1e300, -1e300, -1e300};
  bool anyBox = false;
  for (auto& b : baked)
    if (!b.v.empty()) {
      anyBox = true;
      for (int k = 0; k < 3; ++k) {
        allBox[k] = std::min(allBox[k], b.box[k]);
        allBox[k + 3] = std::max(allBox[k + 3], b.box[k + 3]);
      }
    }
  for (auto& b : baked) { totalT += (long long)b.t.size() / 3; totalV += (long long)b.v.size() / 3; }

  std::vector<std::string> warnings;
  warnings.push_back("JT stores no length unit; assumed " + unitName +
                     " (Siemens NX convention) - pass --assume-units to change");
  if (ctx.sawRangeLod) warnings.push_back("range-LOD nodes: kept the first (finest) alternative only");
  if (ctx.sawLod) warnings.push_back("LOD nodes: kept the first alternative only");
  if (ctx.sawUnsupportedShape) warnings.push_back("some shapes had no tessellated LODs and were skipped");
  if (ctx.sawCaseFallback) warnings.push_back("file references matched case-insensitively (Windows-authored names)");
  if (totalT == 0) warnings.push_back("the model produced no triangles");
  if (maxTriangles > 0 && totalT > maxTriangles)
    warnings.push_back("could not reach the " + std::to_string(maxTriangles) +
                       "-triangle budget (" + std::to_string(totalT) +
                       " at the coarsest authored LODs); the tracker will decimate");
  if (totalT > 500000)
    warnings.push_back(std::to_string(totalT) + " triangles - the tracker will decimate; check the result");

  FILE* rep = fopen((outAbs / "report.json").c_str(), "w");
  if (!rep) fail("cannot write report.json");
  fprintf(rep, "{\n  \"package_version\": %d,\n", PACKAGE_VERSION);
  fprintf(rep, "  \"source\": \"%s\",\n", jsonEscape(inAbs.filename().string()).c_str());
  fprintf(rep, "  \"source_bytes\": %lld,\n", (long long)fs::file_size(inAbs, ec));
  fprintf(rep, "  \"jt_version\": \"%s\",\n", jtVersion.c_str());
  fprintf(rep, "  \"file_length_units\": [],\n");
  fprintf(rep, "  \"assumed_units\": \"%s\",\n", unitName.c_str());
  fprintf(rep, "  \"assumed_units_scale_to_m\": %g,\n", toM);
  fprintf(rep, "  \"output_units\": \"metre\",\n");
  if (anyBox) {
    fprintf(rep, "  \"bbox_m\": [%s, %s, %s, %s, %s, %s],\n",
            num6(allBox[0]).c_str(), num6(allBox[1]).c_str(), num6(allBox[2]).c_str(),
            num6(allBox[3]).c_str(), num6(allBox[4]).c_str(), num6(allBox[5]).c_str());
    fprintf(rep, "  \"size_m\": [%s, %s, %s],\n", num6(allBox[3] - allBox[0]).c_str(),
            num6(allBox[4] - allBox[1]).c_str(), num6(allBox[5] - allBox[2]).c_str());
  } else {
    fprintf(rep, "  \"bbox_m\": [],\n  \"size_m\": [],\n");
  }
  fprintf(rep, "  \"tessellation\": {\"policy\": \"authored LODs, finest within budget\", "
               "\"lod_step_downs\": %d, \"max_triangles\": %d},\n", stepDowns, maxTriangles);
  long long hidden = 0;
  for (auto& p : ctx.parts)
    for (auto& w : p.warnings)
      if (w.find("possibly hidden") != std::string::npos) { hidden++; break; }
  fprintf(rep, "  \"totals\": {\"parts\": %d, \"triangles\": %lld, \"vertices\": %lld, "
               "\"possibly_hidden_parts\": %lld},\n",
          (int)ctx.parts.size(), totalT, totalV, hidden);
  fprintf(rep, "  \"parts\": [");
  for (size_t i = 0; i < ctx.parts.size(); ++i) {
    RawPart& p = ctx.parts[i];
    Baked& b = baked[i];
    fprintf(rep, "%s{\"name\": \"%s\", \"path\": \"%s\", \"triangles\": %lld, \"vertices\": %lld, "
                 "\"color\": [%s, %s, %s], ",
            i ? ", " : "", jsonEscape(p.name).c_str(), jsonEscape(p.path).c_str(),
            (long long)b.t.size() / 3, (long long)b.v.size() / 3,
            num4(p.color[0]).c_str(), num4(p.color[1]).c_str(), num4(p.color[2]).c_str());
    if (!b.v.empty())
      fprintf(rep, "\"bbox_m\": [%s, %s, %s, %s, %s, %s], ", num6(b.box[0]).c_str(),
              num6(b.box[1]).c_str(), num6(b.box[2]).c_str(), num6(b.box[3]).c_str(),
              num6(b.box[4]).c_str(), num6(b.box[5]).c_str());
    else fprintf(rep, "\"bbox_m\": [], ");
    fprintf(rep, "\"lod_index\": %d, \"lod_count\": %d, \"lod_segment_type\": %d, \"warnings\": [",
            p.lods.empty() ? -1 : p.choice, (int)p.lods.size(),
            p.lods.empty() ? 0 : p.lods[p.order[p.choice]].segType);
    for (size_t w = 0; w < p.warnings.size(); ++w)
      fprintf(rep, "%s\"%s\"", w ? ", " : "", jsonEscape(p.warnings[w]).c_str());
    fprintf(rep, "]}");
  }
  fprintf(rep, "],\n  \"external_files\": [");
  for (size_t i = 0; i < ctx.externalFiles.size(); ++i)
    fprintf(rep, "%s\"%s\"", i ? ", " : "", jsonEscape(ctx.externalFiles[i]).c_str());
  fprintf(rep, "],\n");
  fprintf(rep, "  \"files\": [\"%s\", \"%s\", \"%s\", \"report.json\"],\n",
          objName.c_str(), mtlName.c_str(), setName.c_str());
  fprintf(rep, "  \"warnings\": [");
  for (size_t i = 0; i < warnings.size(); ++i)
    fprintf(rep, "%s\"%s\"", i ? ", " : "", jsonEscape(warnings[i]).c_str());
  double tTotal = (double)clock() / CLOCKS_PER_SEC - t0;
  fprintf(rep, "],\n  \"seconds\": {\"read\": %.3f, \"mesh\": %.3f, \"total\": %.3f}\n}\n",
          tRead, tTotal - tRead, tTotal);
  fclose(rep);

  printf("%s: %d part(s), %lld triangles, size [%s, %s, %s] m (units assumed %s), %.3f s -> %s\n",
         inAbs.filename().c_str(), (int)ctx.parts.size(), totalT,
         anyBox ? num6(allBox[3] - allBox[0]).c_str() : "-",
         anyBox ? num6(allBox[4] - allBox[1]).c_str() : "-",
         anyBox ? num6(allBox[5] - allBox[2]).c_str() : "-",
         unitName.c_str(), tTotal, outAbs.c_str());
  for (auto& w : warnings) printf("warning: %s\n", w.c_str());
  return 0;
}
