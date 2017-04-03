from __future__ import print_function
import numpy
import logging

from abopt.engines.pmesh import (
        ParticleMeshEngine,
        ZERO, Literal,
        CodeSegment,
        programme,
        statement,
        ParticleMesh, RealField, ComplexField
        )

from fastpm.perturbation import PerturbationGrowth

class FastPMEngine(ParticleMeshEngine):
    def __init__(self, pm, B=1):
        ParticleMeshEngine.__init__(self, pm)
        fpm = ParticleMesh(Nmesh=pm.Nmesh * B, BoxSize=pm.BoxSize, dtype=pm.dtype, comm=pm.comm)
        self.fengine = ParticleMeshEngine(fpm, q=self.q)

    @programme(ain=['whitenoise'], aout=['dlinear_k'])
    def create_linear_field(engine, whitenoise, powerspectrum, dlinear_k):
        code = CodeSegment(engine)
        code.r2c(real=whitenoise, complex=dlinear_k)
        def tf(k):
            k2 = sum(ki**2 for ki in k)
            r = (powerspectrum(k2 ** 0.5) * (1.0 / engine.pm.BoxSize).prod()) ** 0.5
            r[k2 == 0] = 1.0
            return r
        code.transfer(complex=dlinear_k, tf=tf)
        return code

    @programme(ain=['source_k'], aout=['s'])
    def solve_linear_displacement(engine, source_k, s):
        code = CodeSegment(engine)
        code.decompose(s=Literal(ZERO), layout='layout')
        code.defaults['s'] = numpy.zeros_like(engine.q)
        for d in range(engine.pm.ndim):
            def tf(k, d=d):
                k2 = sum(ki ** 2 for ki in k)
                mask = k2 == 0
                k2[mask] = 1.0
                return 1j * k[d] / k2 * ~mask
            code.assign(x='source_k', y='disp1_k')
            code.transfer(complex='disp1_k', tf=tf)
            code.c2r(complex='disp1_k', real='disp1')
            code.readout(mesh='disp1', value='s1', s=Literal(ZERO))
            code.assign_component(attribute='s', value='s1', dim=d)
        return code

    @statement(ain=['x1', 'x2'], aout='y')
    def bilinear(engine, x1, c1, x2, c2, y):
        y[...] = x1 * c1 + x2 * c2

    @bilinear.defvjp
    def _(engine, _x1, _x2, _y, c1, c2):
        _x1[...] = _y * c1
        _x2[...] = _y * c2

    @programme(ain=['dlinear_k'], aout=['s', 'v'])
    def solve_lpt(engine, cosmo, aend, dlinear_k, s, v):
        code = CodeSegment(engine)
        pt = PerturbationGrowth(cosmo)
        code.solve_linear_displacement(source_k='dlinear_k', s='s1')
        code.generate_2nd_order_source(source_k='dlinear_k', source2_k='source2_k')
        code.solve_linear_displacement(source_k='source2_k', s='s2')

        code.bilinear(x1='s1', c1=pt.D1(aend),
                      x2='s2', c2=pt.D2(aend),
                       y='s')

        code.bilinear(x1='s1', c1=pt.f1(aend) * aend ** 2 * pt.E(aend) * pt.D1(aend),
                      x2='s2', c2=pt.f2(aend) * aend ** 2 * pt.E(aend) * pt.D2(aend),
                       y='s')
        return code

    @programme(ain=['dlinear_k'], aout=['s', 'v'])
    def solve_fastpm(engine, cosmo, asteps, dlinear_k, s, v):
        pt = PerturbationGrowth(cosmo)
        code = CodeSegment(engine)
        code.solve_lpt(cosmo=cosmo, aend=asteps[0], dlinear_k=dlinear_k, s='s', v='v')

        def K(ai, af, ar):
            return 1 / (ar ** 2 * pt.E(ar)) * (pt.Gf(af) - pt.Gf(ai)) / pt.gf(ar)
        def D(ai, af, ar):
            return 1 / (ar ** 3 * pt.E(ar)) * (pt.Gp(af) - pt.Gp(ai)) / pt.gp(ar)

        for ai, af in zip(a[:-1], a[1:]):
            ac = (ai * af) ** 0.5
            self.kick(v=v, f='f', kick_factor=K(ai, ac, ai))
            self.drift(x=x, v=v, drift_fractor=D(ai, ac, ac))
            self.drift(x=x, v=v, drift_factor=D(ac, af, ac))
            self.force_prepare(density_k='density_k', s=s, layout='layout')
            self.force(density_k='density_k', s=s, force='f', force_factor=1.5 * pt.Om0)
            self.kick(v=v, f='f', kick_factor=K(ac, af, af))


    @programme(ain=['source_k'], aout=['source2_k'])
    def generate_2nd_order_source(engine, source_k, source2_k):
        code = CodeSegment(engine)
        if engine.pm.ndim < 3:
            code.defaults['source2_k'] = engine.pm.create(mode='complex', zeros=True)
            return code

        code.defaults['source2'] = engine.pm.create(mode='real', zeros=True)

        D1 = [1, 2, 0]
        D2 = [2, 0, 1]
        varname = ['var_%d' % d for d in range(engine.pm.ndim)]
        for d in range(engine.pm.ndim):
            def tf(k, d=d):
                k2 = sum(ki ** 2 for ki in k)
                mask = k2 == 0
                k2[mask] = 1.0
                return 1j * k[d] * 1j * k[d] / k2 * ~mask
            code.assign(x='source_k', y=varname[d])
            code.transfer(complex=varname[d], tf=tf)
            code.c2r(complex=varname[d], real=varname[d])

        for d in range(engine.pm.ndim):
            code.multiply(x1=varname[D1[d]], x2=varname[D2[d]], y='phi_ii')
            code.add(x1='source2', x2='phi_ii', y='source2')

        for d in range(engine.pm.ndim):
            def tf(k, d=d):
                k2 = sum(ki ** 2 for ki in k)
                mask = k2 == 0
                k2[mask] = 1.0
                return 1j * k[D1[d]] * 1j * k[D2[d]] / k2 * ~mask
            code.assign(x='source_k', y='phi_ij')
            code.transfer(complex='phi_ij', tf=tf)
            code.c2r(complex='phi_ij', real='phi_ij')
            code.multiply(x1='phi_ij', x2='phi_ij', y='phi_ij')
            code.multiply(x1='phi_ij', x2=Literal(-1.0), y='phi_ij')
            code.add(x1='source2', x2='phi_ij', y='source2')

        code.multiply(x1='source2', x2=Literal(3.0 /7), y='source2')
        code.r2c(real='source2', complex='source2_k')
        return code

    @programme(aout=['force'], ain=['s'])
    def force(engine, force, s, force_factor):
        code = CodeSegment(engine)
        code.force_prepare(s=s, density_k='density_k', layout='layout')
        code.force_compute(s=s, density_k='density_k', layout='layout', force=force, 
                force_factor=force_factor)
        return code

    @programme(aout=['density_k', 'layout'], ain=['s'])
    def force_prepare(engine, density_k, s, layout):
        code = CodeSegment(engine.fengine)
        code.decompose(s=s, layout=layout)
        code.paint(s=s, layout=layout, mesh=density_k)
        return code

    @programme(aout=['force'], ain=['density_k', 's', 'layout'])
    def force_compute(engine, force, density_k, s, layout, force_factor):
        code = CodeSegment(engine.fengine)
        code.defaults['force'] = numpy.zeros_like(engine.q)
        def assert_pm(field, pm):
            assert field.pm == pm

        code.inspect(inspector=lambda engine, frontier:
            assert_pm(frontier['density_k'], code.engine.pm))

        for d in range(engine.pm.ndim):
            def tf(k):
                k2 = sum(ki ** 2 for ki in k)
                mask = k2 == 0
                k2[mask] = 1.0
                return 1j * k[d] / k2 * ~mask
            code.assign(x='density_k', y='complex')
            code.transfer(complex='complex', tf=tf)
            code.c2r(complex='complex', real='real')
            code.readout(value='force1', mesh='real', s=s, layout='layout')
            code.assign_component(attribute='force', dim=d, value='force1')
        code.multiply(x1='force', x2=Literal(force_factor), y='force')

        return code

    @statement(aout=['v'], ain=['v', 'f'])
    def kick(engine, v, f, kick_factor):
        v[...] += f * kick_factor

    @kick.defvjp
    def _(engine, _f, _v, kick_factor):
        _f[...] = _v * kick_factor

    @statement(aout=['x'], ain=['x', 'v'])
    def drift(engine, x, v, drift_factor):
        x[...] += v * drift_factor

    @drift.defvjp
    def _(engine, _x, _v, drift_factor):
        _v[...] = _x * drift_factor
