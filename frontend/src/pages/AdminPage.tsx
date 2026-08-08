import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api, type Engineer, type Org } from "../api";
import { clearJobSession, useAuth } from "../auth";

export default function AdminPage() {
  const { user, logout, ready } = useAuth();
  const nav = useNavigate();
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [engineers, setEngineers] = useState<Engineer[]>([]);
  const [orgName, setOrgName] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [orgId, setOrgId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    setOrgs(await api.listOrgs());
    setEngineers(await api.listEngineers());
  };

  useEffect(() => {
    load().catch((e) => setErr(e.message));
  }, []);

  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (user.role !== "super_admin") return <Navigate to="/app" replace />;

  const openBallooning = () => {
    // Start ballooning without carrying over the previous drawing/job
    clearJobSession();
    nav("/app");
  };
  return (
    <div className="page-center">
      <div className="card wide">
        <div className="row-between mb">
          <div>
            <h1>Super Admin</h1>
            <p className="muted">
              Only people you allow can use the app. Create an organization, add engineers, then enable
              Active and permissions (Read / Write / Delete) under User access control.
            </p>
          </div>
          <button className="btn btn-outline btn-sm" onClick={logout}>
            Log out
          </button>
        </div>

        {err && <div className="error">{err}</div>}
        {msg && <div className="ok">{msg}</div>}

        <div className="section">
          <h2>Organizations</h2>
          <div className="row">
            <input
              style={{ flex: 1, borderRadius: 10, padding: 12, border: 0 }}
              placeholder="Organization name (e.g. Acme Engineering)"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
            />
            <button
              className="btn btn-primary"
              style={{ width: "auto" }}
              onClick={async () => {
                try {
                  await api.createOrg(orgName);
                  setOrgName("");
                  setMsg("Organization created");
                  await load();
                } catch (e: any) {
                  setErr(e.message);
                }
              }}
            >
              Create organization
            </button>
          </div>
          <table className="table mt">
            <thead>
              <tr>
                <th>Name</th>
                <th>Tenant ID</th>
                <th>Engineers</th>
                <th>Status</th>
                <th>Trial ends</th>
              </tr>
            </thead>
            <tbody>
              {orgs.map((o) => (
                <tr key={o.id}>
                  <td>{o.name}</td>
                  <td>{o.tenant_id}</td>
                  <td>{o.engineers}</td>
                  <td>{o.status}</td>
                  <td>{o.trial_ends}</td>
                </tr>
              ))}
              {!orgs.length && (
                <tr>
                  <td colSpan={5} className="muted">
                    No organizations yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h2>Engineers</h2>
          <div className="stack">
            <input
              placeholder="Full name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              style={{ borderRadius: 10, padding: 12, border: 0 }}
            />
            <input
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              style={{ borderRadius: 10, padding: 12, border: 0 }}
            />
            <select
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
              style={{ borderRadius: 10, padding: 12, border: 0 }}
            >
              <option value="">Select organization...</option>
              {orgs.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
            <button
              className="btn btn-primary"
              onClick={async () => {
                try {
                  const res: any = await api.addEngineer({
                    full_name: fullName,
                    email,
                    org_id: orgId,
                  });
                  setMsg(`Engineer added. Username ${res.username}, temp password engineer123`);
                  setFullName("");
                  setEmail("");
                  await load();
                } catch (e: any) {
                  setErr(e.message);
                }
              }}
            >
              Add engineer
            </button>
          </div>
          <table className="table mt">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Username</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {engineers.map((e) => (
                <tr key={e.id}>
                  <td>{e.full_name}</td>
                  <td>{e.email}</td>
                  <td>{e.username}</td>
                  <td>{e.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h2>User access control</h2>
          <p className="muted">
            New engineers start Inactive with no permissions. Check Active and Read at minimum, then Save —
            or use Grant full access.
          </p>
          <table className="table mt">
            <thead>
              <tr>
                <th>Name</th>
                <th>Username</th>
                <th>Email</th>
                <th>Read</th>
                <th>Write</th>
                <th>Delete</th>
                <th>Active</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {engineers.map((e) => (
                <AccessRow key={e.id} eng={e} onSaved={load} />
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt">
          <button type="button" className="btn btn-ghost btn-sm" style={{ width: "auto" }} onClick={openBallooning}>
            ← Open ballooning app
          </button>
        </div>
      </div>
    </div>
  );
}

function AccessRow({ eng, onSaved }: { eng: Engineer; onSaved: () => Promise<void> }) {
  const [read, setRead] = useState(!!eng.permissions?.read);
  const [write, setWrite] = useState(!!eng.permissions?.write);
  const [del, setDel] = useState(!!eng.permissions?.delete);
  const [active, setActive] = useState(!!eng.active);

  return (
    <tr>
      <td>{eng.full_name}</td>
      <td>{eng.username}</td>
      <td>{eng.email}</td>
      <td>
        <input type="checkbox" checked={read} onChange={(e) => setRead(e.target.checked)} />
      </td>
      <td>
        <input type="checkbox" checked={write} onChange={(e) => setWrite(e.target.checked)} />
      </td>
      <td>
        <input type="checkbox" checked={del} onChange={(e) => setDel(e.target.checked)} />
      </td>
      <td>
        <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
      </td>
      <td className="row">
        <button
          className="btn btn-ghost btn-sm"
          onClick={async () => {
            await api.updateAccess({
              user_id: eng.id,
              active,
              read,
              write,
              delete: del,
            });
            await onSaved();
          }}
        >
          Save
        </button>
        <button
          className="btn btn-outline btn-sm"
          onClick={async () => {
            await api.grantFull(eng.id);
            await onSaved();
          }}
        >
          Grant full
        </button>
      </td>
    </tr>
  );
}
