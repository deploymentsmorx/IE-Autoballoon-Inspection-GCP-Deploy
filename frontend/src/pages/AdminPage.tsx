import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import {
  api,
  type ApprovedDevice,
  type Customer,
  type Engineer,
  type Installation,
  type Org,
} from "../api";
import { clearJobSession, useAuth } from "../auth";

const inputStyle: React.CSSProperties = {
  borderRadius: 10,
  padding: 12,
  border: 0,
  background: "#0e141c",
  color: "inherit",
  width: "100%",
};

type DeviceRow = ApprovedDevice & { customer_name: string };

export default function AdminPage() {
  const { user, logout, ready } = useAuth();
  const nav = useNavigate();

  const [orgs, setOrgs] = useState<Org[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [engineers, setEngineers] = useState<Engineer[]>([]);
  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [installs, setInstalls] = useState<Installation[]>([]);

  const [companyName, setCompanyName] = useState("");
  const productCode = "costsheet";
  const [licenseKey, setLicenseKey] = useState("");
  const [installPwd, setInstallPwd] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [hostname, setHostname] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [tempPassword, setTempPassword] = useState("engineer123");
  const [requireApproval, setRequireApproval] = useState(true);
  const [grantFull, setGrantFull] = useState(true);

  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const loadLists = async () => {
    const [o, e, c, inst] = await Promise.all([
      api.listOrgs(),
      api.listEngineers(),
      api.listCustomers().catch(() => [] as Customer[]),
      api.listInstallations().catch(() => [] as Installation[]),
    ]);
    setOrgs(o);
    setEngineers(e);
    setCustomers(c);
    setInstalls(inst);

    const devRows: DeviceRow[] = [];
    for (const cust of c) {
      const devs = await api.listApprovedDevices(cust.customer_id).catch(() => []);
      devs.forEach((d) => devRows.push({ ...d, customer_name: cust.organization }));
    }
    setDevices(devRows);
  };

  useEffect(() => {
    loadLists().catch((ex) => setErr(ex instanceof Error ? ex.message : "Load failed"));
  }, []);

  const resetForm = () => {
    setCompanyName("");
    setLicenseKey("");
    setInstallPwd("");
    setDeviceId("");
    setHostname("");
    setFullName("");
    setEmail("");
    setTempPassword("engineer123");
    setRequireApproval(true);
    setGrantFull(true);
  };

  const provision = async () => {
    if (!companyName.trim()) throw new Error("Company / organization name is required");
    if (!fullName.trim() || !email.trim()) throw new Error("Engineer name and email are required");
    if (!email.includes("@")) throw new Error("Valid engineer email is required");
    if (requireApproval && (!deviceId.trim() || !hostname.trim())) {
      throw new Error("Device ID and Host name are required when device approval is enabled");
    }

    const org = await api.createOrg(companyName.trim());
    const customer = await api.createCustomer({
      organization: companyName.trim(),
      license_key: licenseKey.trim() || undefined,
      org_id: org.id,
      install_password: installPwd,
      require_device_approval: requireApproval,
      product_code: productCode,
    });

    if (deviceId.trim() && hostname.trim()) {
      await api.addApprovedDevice(customer.customer_id, deviceId.trim(), hostname.trim());
    }

    const eng = await api.addEngineer({
      full_name: fullName.trim(),
      email: email.trim(),
      org_id: org.id,
      customer_id: customer.customer_id,
      password: tempPassword,
    });

    if (grantFull) {
      await api.grantFull(eng.id);
    }

    return {
      org,
      customer,
      eng,
      creds: {
        license: customer.license_key,
        installPwd: installPwd || "(none)",
        deviceId: deviceId.trim().toUpperCase(),
        hostname: hostname.trim(),
        email: email.trim(),
        username: eng.username,
        tempPassword,
        tenantId: org.tenant_id,
      },
    };
  };

  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
  if (user.role !== "super_admin") return <Navigate to="/app" replace />;

  return (
    <div className="page-center" style={{ alignItems: "flex-start", paddingTop: 24, paddingBottom: 40 }}>
      <div className="card wide">
        <div className="row-between mb">
          <div>
            <h1>Super Admin</h1>
            <p className="muted">
              Fill one form to provision organization, license, device, and engineer. All records save to
              the database (AWS) and appear in the lists below.
            </p>
          </div>
          <button className="btn btn-outline btn-sm" onClick={logout}>
            Log out
          </button>
        </div>

        {err && <div className="error mb">{err}</div>}
        {msg && <div className="ok mb">{msg}</div>}

        <div className="section">
          <h2>Provision customer (one form)</h2>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
              gap: 12,
            }}
          >
            <div className="field">
              <label>Company / organization name</label>
              <input
                style={inputStyle}
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
                placeholder="ABC Engineering"
              />
            </div>
            <div className="field">
              <label>License key</label>
              <input
                style={inputStyle}
                value={licenseKey}
                onChange={(e) => setLicenseKey(e.target.value)}
                placeholder="Auto-generated if blank"
              />
            </div>
            <div className="field">
              <label>Install password (EXE setup)</label>
              <input
                type="password"
                style={inputStyle}
                value={installPwd}
                onChange={(e) => setInstallPwd(e.target.value)}
                placeholder="Required on customer PC"
              />
            </div>
            <div className="field">
              <label>Device ID (copy exactly from customer's Activate screen)</label>
              <input
                style={inputStyle}
                value={deviceId}
                onChange={(e) => setDeviceId(e.target.value)}
                placeholder="DEV-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX — must include DEV- prefix"
              />
            </div>
            <div className="field">
              <label>Host name (copy exactly from customer's Activate screen)</label>
              <input
                style={inputStyle}
                value={hostname}
                onChange={(e) => setHostname(e.target.value)}
                placeholder="e.g. DESKTOP-8DDCU07"
              />
            </div>
            <div className="field">
              <label>Engineer full name</label>
              <input
                style={inputStyle}
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Mordhwaj Singh"
              />
            </div>
            <div className="field">
              <label>Engineer email</label>
              <input
                style={inputStyle}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="user@company.com"
              />
            </div>
            <div className="field">
              <label>Temporary password</label>
              <input
                type="password"
                style={inputStyle}
                value={tempPassword}
                onChange={(e) => setTempPassword(e.target.value)}
                placeholder="User must change on first login"
              />
            </div>
          </div>

          <div className="row mt" style={{ gap: 20 }}>
            <label className="row muted" style={{ fontSize: "0.85rem" }}>
              <input
                type="checkbox"
                checked={requireApproval}
                onChange={(e) => setRequireApproval(e.target.checked)}
              />
              Require device (EXE only on approved PC)
            </label>
            <label className="row muted" style={{ fontSize: "0.85rem" }}>
              <input
                type="checkbox"
                checked={grantFull}
                onChange={(e) => setGrantFull(e.target.checked)}
              />
              Grant full access to engineer immediately
            </label>
          </div>

          <button
            className="btn btn-primary mt"
            style={{ width: "auto" }}
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setErr("");
              setMsg("");
              try {
                const { creds } = await provision();
                resetForm();
                await loadLists();
                setMsg(
                  `Send to customer: License ${creds.license}, Install password ${creds.installPwd}, Email ${creds.email}, Temporary password ${creds.tempPassword}. (Device ID / Host stay on their Activate screen.)`,
                );
              } catch (ex: unknown) {
                setErr(ex instanceof Error ? ex.message : "Provision failed");
                await loadLists();
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Saving…" : "Create & save all"}
          </button>
        </div>

        <div className="section">
          <h2>Organizations</h2>
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
                  <td>
                    <code>{o.tenant_id}</code>
                  </td>
                  <td>{o.engineers}</td>
                  <td>{o.status}</td>
                  <td>{o.trial_ends || "—"}</td>
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
          <h2>Customers &amp; licenses</h2>
          <table className="table mt">
            <thead>
              <tr>
                <th>Customer</th>
                <th>Product</th>
                <th>License</th>
                <th>Tenant ID</th>
                <th>Install pwd</th>
                <th>Devices</th>
                <th>Active installs</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {customers.map((c) => (
                <tr key={c.customer_id}>
                  <td>{c.organization}</td>
                  <td>{c.product_code === "costsheet" ? "Cost Sheet" : "Other product"}</td>
                  <td>
                    <code>{c.license_key}</code>
                  </td>
                  <td>{c.tenant_id || "—"}</td>
                  <td>{c.has_install_password ? "Set" : "—"}</td>
                  <td>{c.approved_devices}</td>
                  <td>{c.active_installations}</td>
                  <td>{c.status}</td>
                </tr>
              ))}
              {!customers.length && (
                <tr>
                  <td colSpan={8} className="muted">
                    No customers yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h2>Registered devices</h2>
          <table className="table mt">
            <thead>
              <tr>
                <th>Customer</th>
                <th>Device ID</th>
                <th>Host name</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((d) => (
                <tr key={d.id}>
                  <td>{d.customer_name}</td>
                  <td>
                    <code>{d.device_id}</code>
                  </td>
                  <td>{d.hostname}</td>
                  <td>{d.status}</td>
                </tr>
              ))}
              {!devices.length && (
                <tr>
                  <td colSpan={4} className="muted">
                    No devices registered
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h2>Active installations</h2>
          <table className="table mt">
            <thead>
              <tr>
                <th>Installation</th>
                <th>Customer</th>
                <th>Device ID</th>
                <th>Host name</th>
                <th>Activated</th>
              </tr>
            </thead>
            <tbody>
              {installs.map((i) => {
                const cust = customers.find((c) => c.customer_id === i.customer_id);
                return (
                  <tr key={i.installation_id}>
                    <td>{i.installation_id}</td>
                    <td>{cust?.organization || i.customer_id}</td>
                    <td>
                      <code>{i.device_id}</code>
                    </td>
                    <td>{i.hostname || "—"}</td>
                    <td>{i.activated_at ? new Date(i.activated_at).toLocaleString() : "—"}</td>
                  </tr>
                );
              })}
              {!installs.length && (
                <tr>
                  <td colSpan={5} className="muted">
                    No activations yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="section">
          <h2>Engineers &amp; access</h2>
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
                <AccessRow
                  key={e.id}
                  eng={e}
                  onSaved={async () => {
                    await loadLists();
                  }}
                />
              ))}
              {!engineers.length && (
                <tr>
                  <td colSpan={8} className="muted">
                    No engineers yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="mt">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            style={{ width: "auto" }}
            onClick={() => {
              clearJobSession();
              nav("/app");
            }}
          >
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
        <button
          className="btn btn-ghost btn-sm"
          onClick={async () => {
            if (!confirm(`Remove user ${eng.username}?`)) return;
            await api.deleteEngineer(eng.id);
            await onSaved();
          }}
        >
          Remove
        </button>
      </td>
    </tr>
  );
}
