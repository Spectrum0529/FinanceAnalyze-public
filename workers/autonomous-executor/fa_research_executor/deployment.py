"""Single integration point: FA013 owns registration, routes and scheduler."""
from .protocol import require


class Adapter:
    def __init__(self, development_driver, *, final_evaluator=None):
        self.driver = development_driver
        self.final_evaluator = final_evaluator

    def __call__(self, context, envelope):
        if envelope['phase'] == 'final_test':
            require(self.final_evaluator is not None, 'FINAL_EVALUATOR_UNAVAILABLE')
            return self.final_evaluator(context, envelope)
        return self.driver.run_development(context, envelope)


def install(runtime, *, resolve_inputs, development_driver, final_evaluator=None):
    from finance_platform.core.autonomous_composition import install as core_install
    return core_install(runtime, resolve_inputs=resolve_inputs,
                        adapter=Adapter(development_driver, final_evaluator=final_evaluator))
